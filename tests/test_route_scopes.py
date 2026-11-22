"""Declared authorization scope for every served route (task #1745).

``webui/route_scopes.py`` maps every ``(method, path)`` the hub serves to the scope
its callers need. These tests pin four properties:

* completeness: a served route without an entry fails, and so does an entry that
  matches no served route (so the table cannot rot);
* fail closed: a route with no entry answers 403 and never reaches its handler;
* a frozen differential: for every route and representative identity the wrapped app
  answers what the bare handlers answer, except the error-precedence differences of
  #1745 and the profile ceilings #1865 added to the action routes, both frozen below,
  and the allowed matrix is frozen per profile;
* the table agrees with the central authentication gate in ``server.py``, and every
  route ``server.py`` serves outside ``web_routes()`` is public or own-auth.
"""

from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from unified_agent import config, oauth, profiles, server, storage
from unified_agent.webui import auth, route_scopes, routes, sse
from unified_agent.webui.route_scopes import ROUTE_SCOPES, Access, Scope

OPERATOR = "Desktop_Operator"
TOKENS = {
    "Desktop_Agent2": "fixture-developer-token",
    "Laptop_Agent5": "fixture-cos-token",
    "Desktop_Agent8": "fixture-research-token",
    "server_agent8": "fixture-public-token",
    OPERATOR: "fixture-operator-token",
}
#: Identity per profile under test. Each resolves to a distinct default profile.
IDENTITY_PROFILES = {
    "Desktop_Agent2": profiles.DEVELOPER,
    "Laptop_Agent5": profiles.CHIEF_OF_STAFF,
    "Desktop_Agent8": profiles.RESEARCH,
    "server_agent8": profiles.PUBLIC,
    OPERATOR: profiles.OPERATOR,
}
ALL_PROFILES = (
    profiles.DEVELOPER,
    profiles.STEWARD,
    profiles.CHIEF_OF_STAFF,
    profiles.RESEARCH,
    profiles.OPERATOR,
    profiles.PUBLIC,
    profiles.CLOUD_ASSISTANT,
    profiles.CLOUD_VAULT_STEWARD,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_AGENT_WORKSPACES", "server_agent8:DENY")
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    monkeypatch.delenv("UA_MOBILE_WATCH_ENABLED", raising=False)
    for name in (
        "UA_AGENT_ACCESS_ENABLED",
        "UA_AGENT_ACCESS_READ_ONLY",
        "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        "UA_MANAGED_SECRETS",
        "UA_APPROVAL_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()


def _walk(route_list, prefix: str = ""):
    """Every (method, path) a route list serves, descending into mounted sub-apps.

    HEAD and OPTIONS are not separate entries: HEAD shares its GET handler and
    OPTIONS is answered by the route itself. A route with no method list (an ASGI
    app such as /mcp) serves every method and is keyed ``ANY``.
    """
    for route in route_list:
        path = prefix + getattr(route, "path", "")
        children = getattr(route, "routes", None)
        if isinstance(route, Mount) or children is not None:
            yield from _walk(children or [], path)
            continue
        methods = {
            m for m in (getattr(route, "methods", None) or ()) if m not in {"HEAD", "OPTIONS"}
        }
        for method in sorted(methods) or ["ANY"]:
            yield method, path


@pytest.fixture
def served_routes(monkeypatch) -> set[tuple[str, str]]:
    """The routes of the real hub app, with the OAuth authorization server on."""
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "fixture-passphrase")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent8")
    oauth.reset_provider()
    try:
        app = server.mcp.streamable_http_app()
        assert oauth.get_provider() is not None, "OAuth routes must be mounted for this check"
        # Peel the ASGI wrappers (TrustedListenerProxyHeaders, then
        # HubAuthASGIMiddleware, task #1892) down to the Starlette app.
        while not hasattr(app, "router"):
            app = getattr(app, "_app", None) or app.app
        return set(_walk(app.router.routes))
    finally:
        oauth.reset_provider()


# --- completeness --------------------------------------------------------------------


def test_every_served_route_has_a_declared_scope(served_routes):
    missing = sorted(
        (method, path)
        for method, path in served_routes
        if route_scopes.scope_for(method, path) is None
    )
    assert not missing, (
        "routes served without an entry in webui/route_scopes.py (add one that says who may "
        f"call them): {missing}"
    )


def test_no_stale_scope_entries(served_routes):
    served = set(served_routes)
    stale = sorted(
        (method, path)
        for method, path in ROUTE_SCOPES
        if (method, path) not in served
        and not (method == "GET" and ("ANY", path) in served)
        and not (method == "ANY" and any(p == path for _, p in served))
    )
    assert not stale, f"scope entries that match no served route: {stale}"


def test_webui_routes_alone_are_covered():
    """The route list the webui registers must be covered without the OAuth routes."""
    for method, path in _walk(routes.web_routes()):
        assert route_scopes.scope_for(method, path) is not None, (method, path)


# --- the table is well formed --------------------------------------------------------


def test_scope_constructor_rejects_malformed_scopes():
    with pytest.raises(ValueError, match="unknown handler check"):
        Scope(Access.CAPABILITY, handler=("made-up",))
    with pytest.raises(ValueError, match="names no capability"):
        Scope(Access.CAPABILITY)
    with pytest.raises(ValueError, match="names no capability"):
        Scope(Access.CAPABILITY, handler=("identity", "approval"))
    with pytest.raises(ValueError, match="cannot name a capability"):
        Scope(Access.ANY_AUTHENTICATED, tools=("task_list",))
    with pytest.raises(ValueError, match="cannot name a capability"):
        Scope(Access.ANY_AUTHENTICATED, handler=("human",))
    with pytest.raises(ValueError, match="cannot name a capability"):
        Scope(Access.ANY_AUTHENTICATED, actions=(("plan_manage", "plan_review"),))
    with pytest.raises(ValueError, match="cannot name a capability"):
        Scope(Access.ANY_AUTHENTICATED, operator=True)
    with pytest.raises(ValueError, match="cannot carry"):
        Scope(Access.PUBLIC, operator=True)
    # The operator alone is a capability: a route reserved for the human operator.
    assert Scope(Access.CAPABILITY, operator=True).operator
    with pytest.raises(ValueError, match="must say who gates it"):
        Scope(Access.ANY_AUTHENTICATED, csrf=True)
    with pytest.raises(ValueError, match="cannot carry"):
        Scope(Access.PUBLIC, tools=("task_list",))
    with pytest.raises(ValueError, match="cannot carry"):
        Scope(Access.OWN_AUTH, csrf=True)


def _plan_manage_actions() -> frozenset[str]:
    from unified_agent.tools.plan import _PLAN_ACTIONS

    return frozenset(fn.__name__ for fn in _PLAN_ACTIONS.values())


def test_capability_names_are_registered_tools():
    """A typo would deny every caller (an outage) or silently gate nothing."""
    registered = set(server.REGISTERED_TOOLS)
    for key, scope in ROUTE_SCOPES.items():
        for tool in (*scope.tools, *scope.read_tools, *scope.delegates):
            assert tool in registered, f"{key} names unregistered tool {tool!r}"
        for manager, action in (*scope.actions, *scope.read_actions):
            assert manager in registered, f"{key} names unregistered manager {manager!r}"
            # plan_manage also dispatches actions added after the legacy aliases
            # (plan_close, the joint actions); its own action table names them.
            dispatched = profiles.CONSOLIDATED_LEGACY_TOOLS | _plan_manage_actions()
            assert action in dispatched, (
                f"{key} names {action!r}, which no consolidated manager dispatches"
            )


def test_routes_outside_web_routes_are_public_or_own_auth(served_routes):
    """The scope wrapper guards web_routes() only (PR #1005 review).

    server.py appends the MCP transport, the root redirect and the OAuth
    authorization server itself, outside ``web_routes()``, so nothing enforces a
    capability declared on one of them. Each must say it is public or authenticates
    itself, never claim a capability that would silently go unenforced.
    """
    webui = set(_walk(routes.web_routes()))
    outside = sorted(route for route in served_routes if route not in webui)
    assert ("ANY", "/mcp") in outside and ("POST", "/token") in outside, outside
    for method, path in outside:
        scope = route_scopes.scope_for(method, path)
        assert scope is not None, (method, path)
        assert scope.access in (Access.PUBLIC, Access.OWN_AUTH), (
            f"{method} {path} is served outside web_routes(), so its declared "
            f"{scope.access} scope would not be enforced"
        )


#: Mutating routes that deliberately skip the CSRF guard: bearer-only callers and
#: scripts, never a browser cookie flow. Adding to this set is a reviewable decision.
CSRF_EXEMPT_MUTATIONS = frozenset(
    {
        ("POST", "/api/routines/{routine_id}/fire"),
        ("POST", "/api/session-capture"),
        ("POST", "/api/inbox/session"),
        ("POST", "/api/inbox/session/owed"),
        ("POST", "/api/tasks/next-work-projection"),
    }
)


def test_mutating_authenticated_routes_are_csrf_guarded():
    for (method, path), scope in ROUTE_SCOPES.items():
        if method in {"GET", "HEAD", "OPTIONS"}:
            continue
        if scope.access in (Access.PUBLIC, Access.OWN_AUTH):
            continue
        if (method, path) in CSRF_EXEMPT_MUTATIONS:
            assert not scope.csrf, f"{method} {path} is listed as CSRF-exempt but is guarded"
            continue
        assert scope.csrf, f"{method} {path} mutates without the CSRF guard"


def _central_public(path: str) -> bool:
    """Whether server.py's authentication gate lets this route template through unauthenticated."""
    concrete = re.sub(r"\{[^}]*\}", "x", path)
    if concrete in server._PUBLIC_PATHS:
        return True
    if any(
        concrete == prefix or concrete.startswith(prefix + "/")
        for prefix in server._PUBLIC_PATH_PREFIXES
    ):
        return True
    return oauth.is_oauth_public_path(concrete)


def test_public_entries_agree_with_the_central_authentication_gate():
    for (method, path), scope in ROUTE_SCOPES.items():
        if scope.access is Access.PUBLIC:
            assert _central_public(path), f"{method} {path} is declared public but is gated"
        else:
            assert not _central_public(path), (
                f"{method} {path} is declared {scope.access} but the central gate serves it "
                "without authentication"
            )


# --- fail closed ---------------------------------------------------------------------


def _client(route_list) -> TestClient:
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=route_list)),
        base_url="http://127.0.0.1",
    )


def _bearer(identity: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[identity]}


def test_route_without_an_entry_answers_403_and_never_reaches_its_handler():
    reached: list[str] = []

    async def handler(request: Request):
        reached.append(request.url.path)
        return JSONResponse({"ok": True})

    undeclared = routes._scoped(Route("/api/not-declared", handler, methods=["GET", "POST"]))
    client = _client([undeclared])
    for identity in TOKENS:
        for method in ("GET", "POST"):
            res = client.request(method, "/api/not-declared", headers=_bearer(identity))
            assert res.status_code == 403, (identity, method)
            assert "no declared authorization scope" in res.json()["error"]
    assert reached == []


def test_declared_method_does_not_cover_an_undeclared_method_on_the_same_path():
    reached: list[str] = []

    async def handler(request: Request):
        reached.append(request.method)
        return JSONResponse({"ok": True})

    # /api/whoami is declared for GET only: a POST registered on it has no entry.
    route = routes._scoped(Route("/api/whoami", handler, methods=["GET", "POST"]))
    client = _client([route])
    assert client.get("/api/whoami", headers=_bearer("Desktop_Agent2")).status_code == 200
    res = client.post("/api/whoami", headers=_bearer("Desktop_Agent2"), json={})
    assert res.status_code == 403
    assert reached == ["GET"]


# --- enforcement ---------------------------------------------------------------------


def test_scope_gate_runs_the_guard_before_the_capability_check():
    """An anonymous caller learns nothing about capabilities: 401, never 403."""
    client = _client(routes.web_routes())
    assert client.get("/api/sessions").status_code == 401
    assert client.post("/api/work-products", json={}).status_code == 401
    hostile = {**_bearer("Desktop_Agent2"), "Origin": "https://evil.example"}
    assert client.get("/api/sessions", headers=hostile).status_code == 403


def test_scope_gate_denies_a_profile_without_the_capability_before_the_handler(monkeypatch):
    reached: list[str] = []

    async def handler(request: Request):
        reached.append(request.url.path)
        return JSONResponse({"ok": True})

    route = routes._scoped(Route("/api/sessions", handler, methods=["GET"]))
    client = _client([route])
    allowed = {
        identity: profiles.tool_is_allowed_for(profile, "session_list")
        for identity, profile in IDENTITY_PROFILES.items()
    }
    assert any(allowed.values()) and not all(allowed.values()), "fixture needs both outcomes"
    for identity, is_allowed in allowed.items():
        res = client.get("/api/sessions", headers=_bearer(identity))
        assert res.status_code == (200 if is_allowed else 403), identity
    assert len(reached) == sum(allowed.values())


def test_cookie_session_mutation_needs_the_csrf_token_before_the_handler():
    reached: list[str] = []

    async def handler(request: Request):
        reached.append(request.url.path)
        return JSONResponse({"ok": True})

    route = routes._scoped(Route("/api/graph-canvas", handler, methods=["PUT"]))
    client = _client(
        [routes._scoped(r) for r in routes._declared_routes() if r.path == "/api/session"]
    )
    # Chief of staff: the developer default lacks graph_canvas_write (task #1865).
    login = client.post("/api/session", json={"token": TOKENS["Laptop_Agent5"]})
    assert login.status_code == 200
    csrf = login.json()["csrf"]
    guarded = TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=[route])),
        base_url="http://127.0.0.1",
        cookies=dict(client.cookies),
    )
    assert guarded.put("/api/graph-canvas", json={}).status_code == 403
    assert reached == []
    assert (
        guarded.put("/api/graph-canvas", json={}, headers={auth.CSRF_HEADER: csrf}).status_code
        == 200
    )
    assert reached == ["/api/graph-canvas"]


# --- HTTP profile ceilings on the action routes (task #1865) -------------------------

#: The routes that ran registered-tool logic with no profile ceiling before #1865.
CEILINGED_ACTION_ROUTES = (
    ("POST", "/api/action/task"),
    ("POST", "/api/action/workflow"),
    ("POST", "/api/action/crew-intervene"),
    ("POST", "/api/action/node-session"),
    ("POST", "/api/action/dispatch"),
    ("POST", "/api/action/dispatch-stack"),
    ("POST", "/api/action/dependency"),
    ("POST", "/api/action/handoff"),
    ("POST", "/api/action/cycle"),
    ("POST", "/api/action/plan"),
    ("POST", "/api/action/proposal"),
    ("POST", "/api/action/fleet"),
    ("POST", "/api/action/oauth-revoke"),
    ("POST", "/api/action/harness-config"),
    ("POST", "/api/node/dispatch"),
    ("PUT", "/api/graph-canvas"),
)


def _stub_client(keys) -> tuple[TestClient, list[tuple[str, str]]]:
    """The real scope wrapper around a handler that only records that it ran."""
    reached: list[tuple[str, str]] = []

    def stub(method: str, path: str):
        async def handler(request: Request):
            reached.append((method, path))
            return JSONResponse({"ok": True})

        return routes._scoped(Route(path, handler, methods=[method]))

    return _client([stub(method, path) for method, path in keys]), reached


def test_public_profile_is_refused_on_the_action_routes_over_http():
    """No MCP session id, so the ambient check admitted everything; the ceiling does not."""
    client = _client(routes.web_routes())
    for method, path in CEILINGED_ACTION_ROUTES:
        res = client.request(method, path, headers=_bearer("server_agent8"), json={})
        assert res.status_code == 403, (method, path, res.text)
        assert "unavailable to this profile" in res.json()["error"] or (
            "reserved for the human operator" in res.json()["error"]
        )
    # task_add is on the public allow-list over MCP, so filing a task stays open to it.
    res = client.post("/api/tasks", headers=_bearer("server_agent8"), json={})
    assert res.status_code == 400, res.text


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_every_ceiling_admits_exactly_its_profiles(profile, monkeypatch):
    """Behaviour, not just the table: an identity reaches the handler iff its profile passes.

    One agent identity is moved onto each profile in turn, so steward and the cloud
    profiles, which no fixture identity holds by default, are exercised too.
    """
    monkeypatch.setitem(profiles.DEFAULT_PROFILES_BY_IDENTITY, "Desktop_Agent2", profile)
    keys = [
        k for k, scope in ROUTE_SCOPES.items() if scope.tools or scope.actions or scope.operator
    ]
    client, reached = _stub_client(keys)
    admitted = []
    # The shared guard keeps a cloud-profile identity on its MCP surface: every /api
    # route refuses it before any ceiling, whatever the matrix says.
    on_http = profile not in profiles.CLOUD_PROFILES
    for method, path in keys:
        expected = on_http and _profile_passes(profile, ROUTE_SCOPES[(method, path)])
        res = client.request(method, _concrete(path), headers=_bearer("Desktop_Agent2"), json={})
        assert res.status_code == (200 if expected else 403), (profile, method, path, res.text)
        if expected:
            admitted.append((method, path))
    assert reached == admitted


def test_operator_passes_the_ceiling_its_mcp_profile_lacks():
    """The operator profile cannot call crew_intervene or task_update over MCP by design."""
    assert not profiles.tool_is_allowed_for(profiles.OPERATOR, "crew_intervene")
    assert not profiles.tool_is_allowed_for(profiles.OPERATOR, "task_update")
    operator_routes = [k for k in CEILINGED_ACTION_ROUTES if ROUTE_SCOPES[k].operator]
    assert operator_routes == list(CEILINGED_ACTION_ROUTES)
    client, reached = _stub_client(operator_routes)
    for method, path in operator_routes:
        res = client.request(method, path, headers=_bearer(OPERATOR), json={})
        assert res.status_code == 200, (method, path)
    assert reached == operator_routes


def test_an_agent_on_the_operator_profile_does_not_pass_as_the_operator(monkeypatch):
    """The exemption is the verified operator identity, never the operator profile."""
    monkeypatch.setitem(profiles.DEFAULT_PROFILES_BY_IDENTITY, "Desktop_Agent8", profiles.OPERATOR)
    assert profiles.default_for_identity("Desktop_Agent8") == profiles.OPERATOR
    keys = [
        ("POST", "/api/action/crew-intervene"),
        ("POST", "/api/action/task"),
        ("POST", "/api/action/fleet"),
    ]
    client, reached = _stub_client(keys)
    for method, path in keys:
        res = client.request(method, path, headers=_bearer("Desktop_Agent8"), json={})
        assert res.status_code == 403, (method, path)
    assert reached == []


def test_task_list_reads_are_ceilinged_over_http(monkeypatch):
    """_require_task_list judged the ambient check, which admits all without an MCP session.

    Every profile holds task_list today, so take it off the public allow-list to see
    the ceiling act: both the wrapper and the handler's own check refuse the read.
    """
    monkeypatch.setattr(
        profiles, "PUBLIC_ALLOWED_TOOLS", profiles.PUBLIC_ALLOWED_TOOLS - {"task_list"}
    )
    reads = [
        "/api/goals",
        "/api/goals/1",
        "/api/tasks/1/why-chain",
        "/api/tasks/1/subtree",
        "/api/tasks/1/comments",
        "/api/tasks/search?q=x",
        "/api/org-proposals",
        "/api/org-proposals/x",
    ]
    for route_list in (routes._declared_routes(), routes.web_routes()):
        client = _client(route_list)
        for path in reads:
            res = client.get(path, headers=_bearer("server_agent8"))
            assert res.status_code == 403, (path, res.status_code, res.text)
        res = client.post(
            "/api/tasks/next-work-projection", headers=_bearer("server_agent8"), content=b"{}"
        )
        assert res.status_code == 403
        assert client.get("/api/goals", headers=_bearer("Desktop_Agent2")).status_code != 403


# --- the allowed matrix --------------------------------------------------------------

#: Profiles that may pass the profile ceiling of each route (every ``tools`` entry by
#: ``profiles.tool_is_allowed_for`` and every ``actions`` entry by
#: ``profiles.consolidated_action_is_allowed_for``), plus ``+operator`` where the verified
#: human operator passes it whatever the operator profile holds. Changing a profile's
#: tool set or a route's capability must change this on purpose.
_P = {
    "dev": profiles.DEVELOPER,
    "stw": profiles.STEWARD,
    "cos": profiles.CHIEF_OF_STAFF,
    "res": profiles.RESEARCH,
    "opr": profiles.OPERATOR,
    "pub": profiles.PUBLIC,
    "cla": profiles.CLOUD_ASSISTANT,
    "cvs": profiles.CLOUD_VAULT_STEWARD,
}


def _profile_passes(profile: str, scope: Scope) -> bool:
    if not (scope.tools or scope.actions):
        return False  # an operator-only route: no profile passes on its own
    return all(profiles.tool_is_allowed_for(profile, tool) for tool in scope.tools) and all(
        profiles.consolidated_action_is_allowed_for(profile, manager, action)
        for manager, action in scope.actions
    )


def _allowed_profiles(scope: Scope) -> str:
    allowed = [short for short, name in _P.items() if _profile_passes(name, scope)]
    if scope.operator:
        allowed.append("+operator")
    return " ".join(allowed)


def test_allowed_matrix_is_frozen():
    actual = {
        f"{method} {path}": _allowed_profiles(scope)
        for (method, path), scope in ROUTE_SCOPES.items()
        if scope.tools or scope.actions or scope.operator
    }
    assert actual == FROZEN_MATRIX, "the profile matrix of a capability route changed"


FROZEN_MATRIX: dict[str, str] = {
    "GET /api/events/stream": "dev stw cos res opr",
    "GET /api/goals": "dev stw cos res opr pub cla cvs",
    "GET /api/goals/{goal_id}": "dev stw cos res opr pub cla cvs",
    "GET /api/inbox/summary": "dev stw cos res cla cvs",
    "GET /api/ledger/rows": "dev stw cos res opr",
    "GET /api/org-proposals": "dev stw cos res opr pub cla cvs",
    "GET /api/org-proposals/{proposal_id}": "dev stw cos res opr pub cla cvs",
    "GET /api/sessions": "dev stw cos res opr",
    "GET /api/sessions/{id}": "dev stw cos res opr",
    # Runs pane (task #1652): the run object reader's ceiling; the verified
    # operator passes it so the Control Center can render the pane.
    "GET /api/runs": "dev stw cos +operator",
    "GET /api/runs/{run_id}": "dev stw cos +operator",
    "GET /api/sessions/{id}/crew": "dev stw cos res opr",
    # Idle-session wake grant (task #2050): the verified human operator's alone.
    "POST /api/sessions/{id}/wake-grant": "+operator",
    # Wake decline (task #2514): the verified human operator's alone, like the grant.
    "POST /api/sessions/{id}/wake-decline": "+operator",
    # Work-proposal reads (task #2514): the coordination_manage read actions, or the
    # verified operator. Review-hold release or keep is the operator's alone.
    "GET /api/work-proposals": "dev cos +operator",
    "GET /api/work-proposals/{proposal_id}": "dev cos +operator",
    "POST /api/action/review-hold": "+operator",
    "GET /api/tasks/search": "dev stw cos res opr pub cla cvs",
    # Task subtree holds (task #1747): read with task_list; hold with task_update and
    # the coordination_manage task_hold action, or as the verified operator.
    "GET /api/tasks/holds": "dev stw cos res opr pub cla cvs",
    "POST /api/tasks/holds": "dev cos +operator",
    "POST /api/tasks/holds/{hold_id}/release": "dev cos +operator",
    "GET /api/tasks/{task_id}/comments": "dev stw cos res opr pub cla cvs",
    "GET /api/tasks/{task_id}/why-chain": "dev stw cos res opr pub cla cvs",
    "GET /api/work-graph": "dev stw cos res opr pub cla cvs",
    "POST /api/action/approval": "dev stw cos opr",
    "POST /api/action/crew-intervene": "dev cos +operator",
    "POST /api/action/cycle": "dev cos +operator",
    "POST /api/action/dependency": "dev cos res +operator",
    "POST /api/action/dispatch": "dev cos +operator",
    "POST /api/action/dispatch-stack": "dev cos +operator",
    "POST /api/action/fleet": "+operator",
    "POST /api/action/handoff": "dev cos +operator",
    "POST /api/action/harness-config": "+operator",
    "POST /api/action/node-followup": "dev stw cos opr",
    "POST /api/action/node-session": "dev cos +operator",
    # Device acceptance (task #2112): the coordination_manage action twin, or the
    # verified operator; node_acceptance.authorize then admits the node's own agent.
    "POST /api/action/node-acceptance": "dev cos +operator",
    # Node retire (task #2208): the coordination_manage node_retire/node_unretire twin,
    # or the verified operator; node_registry then admits lifecycle operators only.
    "POST /api/action/node-retire": "dev cos +operator",
    # Work proposals (task #2393): the coordination_manage work_proposal_decide twin,
    # or the verified operator; work_proposals then admits a human identity only.
    "POST /api/action/work-proposal-decide": "dev cos +operator",
    "POST /api/action/oauth-revoke": "stw cos +operator",
    "POST /api/action/plan": "dev cos +operator",
    "POST /api/action/session-credential-revoke": "+operator",
    "POST /api/action/proposal": "dev stw cos res +operator",
    "POST /api/action/task": "dev stw cos +operator",
    "POST /api/action/workflow": "dev cos +operator",
    "GET /api/nodes/native-sessions": "dev stw cos opr",
    "POST /api/action/node-resume": "dev stw cos opr",
    "POST /api/action/node-adopt": "dev stw cos opr",
    "POST /api/approvals/request": "dev stw cos opr",
    # Approval Ledger re-request (task #1854): the coordination_manage action twin,
    # or the verified operator; github_app_worker then admits lifecycle operators.
    "POST /api/approvals/ledger-rerequest": "dev cos +operator",
    # Approval Ledger poll-now (task #1867): same twin and operator pattern.
    "POST /api/approvals/ledger-poll": "dev cos +operator",
    "POST /api/evidence": "dev stw cos res opr",
    "POST /api/goals": "dev stw cos",
    "POST /api/inbox/session": "dev stw cos res",
    "POST /api/inbox/session/owed": "dev stw cos res",
    "POST /api/node/dispatch": "dev cos +operator",
    "POST /api/tasks": "dev stw cos res pub +operator",
    "POST /api/tasks/next-work-projection": "dev stw cos res opr pub cla cvs",
    "POST /api/tasks/{task_id}/comments": "dev stw cos",
    "POST /api/work-products": "stw cos",
    "POST /api/work-products/{id}/release": "stw cos",
    "PUT /api/graph-canvas": "stw cos +operator",
    # Team snapshot and restore (task #1738): the coordination_manage action twins;
    # restore also needs dispatch authority (delegation_manage delegate) and crew_view.
    "GET /api/teams/snapshots": "dev cos +operator",
    "POST /api/teams/snapshots": "dev cos +operator",
    "GET /api/teams/snapshots/{snapshot_id}/preview": "dev cos +operator",
    "POST /api/teams/snapshots/{snapshot_id}/restore": "dev cos +operator",
    # Messages pane (task #1864): the MCP message_list / message_send / session_list
    # ceilings, plus the verified human operator, whose profile holds no message tools.
    "GET /api/messages/conversations": "dev stw cos res cla cvs +operator",
    "GET /api/messages/conversations/{conversation_id}": "dev stw cos res cla cvs +operator",
    # Conversation handoff (task #1426): message_list and task_add, or the operator.
    "POST /api/messages/conversations/{conversation_id}/handoff": "dev stw cos res +operator",
    "GET /api/messages/conversations/{conversation_id}/handoffs": "dev stw cos res cla cvs +operator",
    "GET /api/messages/sessions": "dev stw cos res opr +operator",
    "POST /api/messages": "dev stw cos res cla cvs +operator",
    # Control Center v2 knowledge and work adapters (task #2469).
    "GET /api/memory/note": "dev stw cos res opr cla cvs +operator",
    "POST /api/action/memory/save": "dev stw cos res +operator",
    "POST /api/action/memory/delete": "dev cos +operator",
    "POST /api/action/memory/restore": "dev cos +operator",
    "POST /api/action/memory/promote": "dev stw cos +operator",
    "POST /api/action/memory/feedback": "dev stw cos res +operator",
    "GET /api/vault/list": "dev stw cos res cvs +operator",
    "POST /api/action/vault/write": "dev stw cos res cvs +operator",
    "POST /api/action/vault/journal": "dev stw cos res cvs +operator",
    "POST /api/action/plan/create": "dev cos res +operator",
    "POST /api/action/plan/update": "dev cos res +operator",
    "POST /api/action/plan/checkpoint": "dev cos res +operator",
    "POST /api/action/plan/resume": "dev cos res +operator",
    "POST /api/action/plan/retry": "dev cos +operator",
    "POST /api/action/plan/claim": "dev cos +operator",
    "POST /api/action/plan/handoff": "dev cos +operator",
    "POST /api/action/plan/close": "dev cos +operator",
    "POST /api/action/plan/delete": "dev cos +operator",
    "POST /api/action/plan/joint/bind": "dev cos +operator",
    "POST /api/action/plan/joint/propose": "dev cos +operator",
    "POST /api/action/plan/joint/review": "dev cos +operator",
    "POST /api/action/plan/joint/decide": "dev cos +operator",
    "POST /api/action/plan/joint/recover": "dev cos +operator",
    "GET /api/plans/{plan_id}/joint": "dev cos +operator",
    "POST /api/action/task/claim": "dev cos +operator",
    "POST /api/action/task/delete": "dev cos +operator",
    "POST /api/action/session/end": "dev stw cos +operator",
    # Control Center v2 hub operations (task #2480).
    "POST /api/action/plan/step-add": "dev cos res +operator",
    "POST /api/action/plan/step-edit": "dev cos res +operator",
    "POST /api/action/plan/step-remove": "dev cos res +operator",
    "POST /api/action/plan/reorder": "dev cos res +operator",
    "POST /api/action/plan/edit": "dev cos res +operator",
    "POST /api/action/goal/edit": "dev stw cos +operator",
    "POST /api/action/goal/link": "dev stw cos +operator",
    "POST /api/action/goal/unlink": "dev stw cos +operator",
    "POST /api/action/goal/close": "dev stw cos +operator",
    "POST /api/action/goal/reopen": "dev stw cos +operator",
    "GET /api/routines": "cos +operator",
    "POST /api/action/routine/add": "cos +operator",
    "POST /api/action/routine/pause": "cos +operator",
    "POST /api/action/routine/resume": "cos +operator",
    "POST /api/action/routine/run": "cos +operator",
    "POST /api/action/langgraph/run": "dev stw cos +operator",
    "POST /api/action/session/operator-end": "+operator",
    "GET /api/launch-attempts/{key}": "dev cos +operator",
    # Unsettled keyed spawn recovery (review #1521 pass 5): the verified operator's alone.
    "POST /api/dispatch-runs/{run_id}/settle-spawn": "+operator",
    # Control Center v2 agents (task #2470): enrolment is the verified human
    # operator's alone; connectors and org proposals take their tool's ceiling.
    "GET /api/agent-roster": "+operator",
    # Task #2523: an agent's profile ceiling and assigned skills take skill_registry's
    # ceiling, as the skills catalog read does.
    "GET /api/agents/{label}/profile": "stw cos +operator",
    "POST /api/action/agent/add": "+operator",
    "POST /api/action/agent/remove": "+operator",
    "POST /api/action/connector/register": "dev stw cos +operator",
    "POST /api/action/connector/register-package": "dev stw cos +operator",
    "POST /api/action/connector/authorize": "dev stw cos +operator",
    "POST /api/action/connector/revoke": "dev stw cos +operator",
    "POST /api/action/org-proposal": "dev stw cos +operator",
    # Control Center v2 hub gaps (task #2516): the subtree read takes why-chain's
    # task_list ceiling; the uncapped evidence export takes evidence_report's; the
    # session credential list is the verified human operator's alone.
    "GET /api/tasks/{task_id}/subtree": "dev stw cos res opr pub cla cvs",
    "GET /api/evidence/export": "dev stw cos +operator",
    "GET /api/session-credentials": "+operator",
}


# --- no behaviour change: wrapped routes answer what the bare handlers answer --------


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "1", path)


@pytest.fixture
def finite_stream(monkeypatch):
    """An SSE body never completes: swap in a finite generator for the differential run."""

    def finite(*args, **kwargs):
        async def empty():
            if False:
                yield {}

        return empty()

    monkeypatch.setattr(sse, "event_stream", finite)


#: (method, path, identity) -> (bare handler status, wrapped status). These are error
#: precedence only, for a caller the handler would refuse anyway: the handler validates the
#: request body (400) before it checks the profile, so an empty body used to answer 400 to a
#: profile that lacks the route's capability. The wrapper answers 403 first. A request that
#: any handler used to accept, or refuse for the profile, answers the same.
EXPECTED_PRECEDENCE_DIFFS: dict[tuple[str, str, str], tuple[int, int]] = {
    ("POST", "/api/approvals/request", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/approvals/request", "server_agent8"): (400, 403),
    ("POST", "/api/evidence", "server_agent8"): (400, 403),
    ("POST", "/api/goals", "Desktop_Operator"): (400, 403),
    ("POST", "/api/goals", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/goals", "server_agent8"): (400, 403),
    ("POST", "/api/work-products", "Desktop_Operator"): (400, 403),
    ("POST", "/api/work-products", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/work-products", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/work-products", "server_agent8"): (400, 403),
}


#: (method, path, identity) -> (bare handler status, wrapped status) for the profile
#: ceilings task #1865 put on the action routes. Each is a caller whose profile cannot
#: call the route's MCP twin, so the wrapper refuses it before the handler: research
#: (``Desktop_Agent8``) and public (``server_agent8``) lose the agent-dispatch and task-write
#: routes, the developer default loses the canvas write and device sign-in revoke, and
#: fleet control and harness publish are the operator's alone. ``PUT /api/graph-canvas``
#: answered 200 before (an empty scene is a valid write). The operator identity appears
#: nowhere: it passes every ceiling marked ``operator``.
EXPECTED_CEILING_DIFFS: dict[tuple[str, str, str], tuple[int, int]] = {
    ("POST", "/api/action/crew-intervene", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/crew-intervene", "server_agent8"): (400, 403),
    ("POST", "/api/action/cycle", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/cycle", "server_agent8"): (400, 403),
    ("POST", "/api/action/dependency", "server_agent8"): (400, 403),
    ("POST", "/api/action/dispatch", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/dispatch", "server_agent8"): (400, 403),
    ("POST", "/api/action/dispatch-stack", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/dispatch-stack", "server_agent8"): (400, 403),
    ("POST", "/api/action/fleet", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/fleet", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/fleet", "Laptop_Agent5"): (400, 403),
    ("POST", "/api/action/fleet", "server_agent8"): (400, 403),
    ("POST", "/api/action/handoff", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/handoff", "server_agent8"): (400, 403),
    ("POST", "/api/action/harness-config", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/harness-config", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/harness-config", "Laptop_Agent5"): (400, 403),
    ("POST", "/api/action/harness-config", "server_agent8"): (400, 403),
    ("POST", "/api/action/node-session", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/node-session", "server_agent8"): (400, 403),
    ("POST", "/api/action/node-acceptance", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/node-acceptance", "server_agent8"): (400, 403),
    ("POST", "/api/action/node-retire", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/node-retire", "server_agent8"): (400, 403),
    ("POST", "/api/action/work-proposal-decide", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/work-proposal-decide", "server_agent8"): (400, 403),
    ("POST", "/api/action/oauth-revoke", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/oauth-revoke", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/oauth-revoke", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan", "server_agent8"): (400, 403),
    ("POST", "/api/action/proposal", "server_agent8"): (400, 403),
    ("POST", "/api/action/task", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/task", "server_agent8"): (400, 403),
    ("POST", "/api/action/workflow", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/workflow", "server_agent8"): (400, 403),
    ("POST", "/api/approvals/ledger-rerequest", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/approvals/ledger-rerequest", "server_agent8"): (400, 403),
    ("POST", "/api/approvals/ledger-poll", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/approvals/ledger-poll", "server_agent8"): (400, 403),
    ("POST", "/api/node/dispatch", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/node/dispatch", "server_agent8"): (400, 403),
    ("PUT", "/api/graph-canvas", "Desktop_Agent2"): (200, 403),
    ("PUT", "/api/graph-canvas", "Desktop_Agent8"): (200, 403),
    ("PUT", "/api/graph-canvas", "server_agent8"): (200, 403),
}


#: The Control Center v2 adapters (task #2469). Without the wrapper the guarded tool
#: call itself refuses these callers (a ToolError, 400); the wrapper answers 403 first.
EXPECTED_CC_DIFFS: dict[tuple[str, str, str], tuple[int, int]] = {
    # The complete evidence export (task #2516): the bare handler refuses the
    # missing format first; the evidence_report ceiling refuses these profiles.
    ("GET", "/api/evidence/export", "Desktop_Agent8"): (400, 403),
    ("GET", "/api/evidence/export", "server_agent8"): (400, 403),
    ("GET", "/api/memory/note", "server_agent8"): (400, 403),
    ("GET", "/api/plans/{plan_id}/joint", "Desktop_Agent8"): (400, 403),
    ("GET", "/api/plans/{plan_id}/joint", "server_agent8"): (400, 403),
    ("GET", "/api/vault/list", "server_agent8"): (400, 403),
    ("POST", "/api/action/memory/delete", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/memory/delete", "server_agent8"): (400, 403),
    ("POST", "/api/action/memory/feedback", "server_agent8"): (400, 403),
    ("POST", "/api/action/memory/promote", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/memory/promote", "server_agent8"): (400, 403),
    ("POST", "/api/action/memory/restore", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/memory/restore", "server_agent8"): (400, 403),
    ("POST", "/api/action/memory/save", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/checkpoint", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/claim", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/claim", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/close", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/close", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/create", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/delete", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/delete", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/handoff", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/handoff", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/bind", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/bind", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/decide", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/decide", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/propose", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/propose", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/recover", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/recover", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/review", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/joint/review", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/resume", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/retry", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/plan/retry", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/update", "server_agent8"): (400, 403),
    ("POST", "/api/action/session/end", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/session/end", "server_agent8"): (400, 403),
    ("POST", "/api/action/task/claim", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/task/claim", "server_agent8"): (400, 403),
    ("POST", "/api/action/task/delete", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/task/delete", "server_agent8"): (400, 403),
    ("POST", "/api/action/vault/journal", "server_agent8"): (400, 403),
    ("POST", "/api/action/vault/write", "server_agent8"): (400, 403),
    # Task #2480 routes.
    ("GET", "/api/routines", "Desktop_Agent2"): (400, 403),
    ("GET", "/api/routines", "Desktop_Agent8"): (400, 403),
    ("GET", "/api/routines", "server_agent8"): (400, 403),
    ("POST", "/api/action/goal/close", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/goal/close", "server_agent8"): (400, 403),
    ("POST", "/api/action/goal/edit", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/goal/edit", "server_agent8"): (400, 403),
    ("POST", "/api/action/goal/link", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/goal/link", "server_agent8"): (400, 403),
    ("POST", "/api/action/goal/reopen", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/goal/reopen", "server_agent8"): (400, 403),
    ("POST", "/api/action/goal/unlink", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/goal/unlink", "server_agent8"): (400, 403),
    ("POST", "/api/action/langgraph/run", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/langgraph/run", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/edit", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/reorder", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/step-add", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/step-edit", "server_agent8"): (400, 403),
    ("POST", "/api/action/plan/step-remove", "server_agent8"): (400, 403),
    ("POST", "/api/action/routine/add", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/routine/add", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/routine/add", "server_agent8"): (400, 403),
    ("POST", "/api/action/routine/pause", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/routine/pause", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/routine/pause", "server_agent8"): (400, 403),
    # Task #2501: the launch-attempt lookup takes the dispatch capability.
    ("GET", "/api/launch-attempts/{key}", "Desktop_Agent8"): (200, 403),
    ("GET", "/api/launch-attempts/{key}", "server_agent8"): (200, 403),
    ("POST", "/api/action/routine/resume", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/routine/resume", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/routine/resume", "server_agent8"): (400, 403),
    ("POST", "/api/action/routine/run", "Desktop_Agent2"): (400, 403),
    ("POST", "/api/action/routine/run", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/routine/run", "server_agent8"): (400, 403),
    # Task #2514 routes: the coordination_manage read ceiling refuses these profiles.
    ("GET", "/api/work-proposals", "Desktop_Agent8"): (400, 403),
    ("GET", "/api/work-proposals", "server_agent8"): (400, 403),
    ("GET", "/api/work-proposals/{proposal_id}", "Desktop_Agent8"): (400, 403),
    ("GET", "/api/work-proposals/{proposal_id}", "server_agent8"): (400, 403),
    # Task #2470: the org-proposal adapter. The connector handlers check their
    # roles themselves (#2517), so they answer 403 bare too: no difference.
    ("POST", "/api/action/org-proposal", "Desktop_Agent8"): (400, 403),
    ("POST", "/api/action/org-proposal", "server_agent8"): (400, 403),
    # Task #2523: the bare handler answers 404 for the unknown fixture label "1"; the
    # skill_registry ceiling refuses these profiles first.
    ("GET", "/api/agents/{label}/profile", "Desktop_Agent2"): (404, 403),
    ("GET", "/api/agents/{label}/profile", "Desktop_Agent8"): (404, 403),
    ("GET", "/api/agents/{label}/profile", "server_agent8"): (404, 403),
}


#: The reverse case (task #2469): the wrapper carries the operator's declared action
#: into the call, so the operator reaches the tool, which answers 404 for the fixture
#: plan id. The bare handler has no route authority and plan_manage refuses (400).
EXPECTED_OPERATOR_AUTHORITY_DIFFS: dict[tuple[str, str, str], tuple[int, int]] = {
    ("GET", "/api/plans/{plan_id}/joint", "Desktop_Operator"): (400, 404),
    # Task #2480: routine_list is reached through the route's declared tool.
    ("GET", "/api/routines", "Desktop_Operator"): (400, 200),
    # Task #2514: work-proposal reads reach coordination_manage through the route's
    # declared action; the fixture id "1" is not a proposal (404).
    ("GET", "/api/work-proposals", "Desktop_Operator"): (400, 200),
    ("GET", "/api/work-proposals/{proposal_id}", "Desktop_Operator"): (400, 404),
}


def _differential(identities, *, skip_open_reads=False):
    bare = _client(routes._declared_routes())
    wrapped = _client(routes.web_routes())
    diffs: dict[tuple[str, str, str], tuple[int, int]] = {}
    for (method, path), scope in sorted(ROUTE_SCOPES.items()):
        if (
            method == "ANY"
            or path in ("/mcp", "/")
            or not path.startswith(("/api", "/ui", "/health", "/oauth/conn"))
        ):
            continue
        if scope.access is Access.ANY_AUTHENTICATED and method == "GET":
            if skip_open_reads:
                continue
            callers = [None]  # the wrapper adds only the guard the handler already runs
        else:
            callers = [None, *IDENTITY_PROFILES]
        for identity in callers:
            headers = _bearer(identity) if identity else {}
            kwargs = {"json": {}} if method in {"POST", "PUT"} else {}
            old = bare.request(method, _concrete(path), headers=headers, **kwargs).status_code
            new = wrapped.request(method, _concrete(path), headers=headers, **kwargs).status_code
            if old != new:
                diffs[(method, path, identity or "anonymous")] = (old, new)
    return diffs


def test_wrapped_routes_answer_what_the_bare_handlers_answer(finite_stream, monkeypatch):
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    diffs = _differential(IDENTITY_PROFILES)
    assert diffs == {
        **EXPECTED_PRECEDENCE_DIFFS,
        **EXPECTED_CEILING_DIFFS,
        **EXPECTED_CC_DIFFS,
        **EXPECTED_OPERATOR_AUTHORITY_DIFFS,
    }
    # Every difference is a capability denial: the caller's profile cannot pass the
    # route's ceiling and the caller is not the operator the route admits.
    for (method, path, identity), (_old, new) in diffs.items():
        if (method, path, identity) in EXPECTED_OPERATOR_AUTHORITY_DIFFS:
            assert ROUTE_SCOPES[(method, path)].operator and identity == OPERATOR
            continue
        profile = IDENTITY_PROFILES[identity]
        scope = ROUTE_SCOPES[(method, path)]
        assert new == 403
        assert not _profile_passes(profile, scope)
        assert not (scope.operator and identity == OPERATOR)
    for key in EXPECTED_PRECEDENCE_DIFFS:
        assert diffs[key] == (400, 403)


#: Warn mode only (task #2480): ``GET /api/routines`` takes no required parameter, so
#: the bare handler's ``routine_list`` call runs (no profile exposure without
#: enforcement) and answers 200; the wrapper's scope still refuses these profiles
#: and admits the operator, so the operator's row is no difference at all.
EXPECTED_WARN_MODE_OVERRIDES: dict[tuple[str, str, str], tuple[int, int]] = {
    ("GET", "/api/routines", identity): (200, 403)
    for identity in ("Desktop_Agent2", "Desktop_Agent8", "server_agent8")
}


def test_wrapped_routes_answer_the_same_in_warn_mode(finite_stream, monkeypatch):
    """Warn mode lets anonymous callers reach the handlers: the wrapper must not change that."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    diffs = _differential(IDENTITY_PROFILES, skip_open_reads=True)
    expected = {
        **EXPECTED_PRECEDENCE_DIFFS,
        **EXPECTED_CEILING_DIFFS,
        **EXPECTED_CC_DIFFS,
        **EXPECTED_OPERATOR_AUTHORITY_DIFFS,
        **EXPECTED_WARN_MODE_OVERRIDES,
    }
    # Without enforcement the bare routine_list call also answers the operator 200.
    del expected[("GET", "/api/routines", OPERATOR)]
    assert diffs == expected
    assert not any(identity == "anonymous" for _, _, identity in diffs)


# The route's declared authority must survive its guarded downstream invocation.
def test_operator_route_updates_task_but_keeps_missing_session_mcp_restricted():
    from unified_agent.tools import tasks

    task = tasks.task_add(title="route-bound update fixture")
    client = _client(routes.web_routes())
    response = client.post(
        "/api/action/task",
        headers=_bearer(OPERATOR),
        json={"id": task["id"], "title": "updated through authorized route"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "updated through authorized route"
    identity = config.agent_identity_var.set(OPERATOR)
    session = config.mcp_session_id_var.set(None)
    try:
        assert profiles.current() == profiles.OPERATOR
        assert not profiles.tool_is_allowed("task_update")
        assert not profiles.tool_is_allowed("message_send")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_operator_route_authority_keeps_task_update_approval(monkeypatch, tmp_path):
    from unified_agent.tools import approvals, tasks

    task = tasks.task_add(title="approval remains required")
    policy_file = tmp_path / "approve-task-update.yaml"
    policy_file.write_text(
        "rules:\n  - name: approve_task_update\n    type: tool\n"
        "    tool: task_update\n    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    client = _client(routes.web_routes())
    response = client.post(
        "/api/action/task",
        headers=_bearer(OPERATOR),
        json={"id": task["id"], "title": "must not be written"},
    )
    assert response.status_code == 202, response.text
    pending = approvals.approval_pending()["pending"]
    assert len(pending) == 1 and pending[0]["tool"] == "task_update"
    assert tasks.task_list()["tasks"][0]["title"] == "approval remains required"


@pytest.mark.parametrize("identity", [OPERATOR, "Desktop_Agent2"])
def test_authorized_route_legacy_action_is_exact_and_identity_bound(identity, monkeypatch):
    from unified_agent.webui import api

    effects = []

    def handoff():
        effects.append("handoff")
        # Call authority does not widen discovery or unrelated tools.
        assert "handoff" not in profiles.allowed_tools({"handoff"})
        assert not profiles.tool_is_allowed("routine_list")
        changed = config.agent_identity_var.set("server_agent8")
        try:
            assert not profiles.tool_is_allowed("handoff")
        finally:
            config.agent_identity_var.reset(changed)
        return {"ok": True}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS, "handoff", server._guarded_and_instrumented(handoff)
    )

    async def handler(request):
        return JSONResponse(await api.call_tool("handoff"))

    client = _client([routes._scoped(Route("/api/action/handoff", handler, methods=["POST"]))])
    response = client.post("/api/action/handoff", headers=_bearer(identity), json={})
    assert response.status_code == 200, response.text
    assert effects == ["handoff"]
    # Public identity does not receive a grant from the same route.
    refused = client.post("/api/action/handoff", headers=_bearer("server_agent8"), json={})
    assert refused.status_code == 403
    assert effects == ["handoff"]
    token = config.agent_identity_var.set(identity)
    try:
        assert not profiles.tool_is_allowed("handoff")
    finally:
        config.agent_identity_var.reset(token)


def test_operator_route_consolidated_authority_is_action_bound(monkeypatch):
    from unified_agent.webui import api

    def plan_manage(action):
        assert action == "review"
        assert profiles.consolidated_action_is_allowed("plan_manage", "plan_review")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_create")
        return {"ok": True}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS, "plan_manage", server._guarded_and_instrumented(plan_manage)
    )

    async def handler(request):
        return JSONResponse(await api.call_tool("plan_manage", action="review"))

    client = _client([routes._scoped(Route("/api/action/plan", handler, methods=["POST"]))])
    response = client.post("/api/action/plan", headers=_bearer(OPERATOR), json={})
    assert response.status_code == 200, response.text
    token = config.agent_identity_var.set(OPERATOR)
    try:
        assert not profiles.tool_is_allowed("plan_manage")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_review")
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("identity", list(IDENTITY_PROFILES))
def test_authenticated_cycle_reads_return_stored_content(identity):
    from unified_agent.tools.cycles import autonomous_cycle_start

    created = autonomous_cycle_start("Desktop_Agent2")
    cycle_id = created["cycle"]["id"]
    client = _client(routes.web_routes())
    listed = client.get("/api/cycles", headers=_bearer(identity))
    assert listed.status_code == 200, listed.text
    assert "error" not in listed.json()
    assert cycle_id in [cycle["id"] for cycle in listed.json()["cycles"]]
    detail = client.get(f"/api/cycles/{cycle_id}", headers=_bearer(identity))
    assert detail.status_code == 200, detail.text
    assert detail.json()["cycle"]["id"] == cycle_id
    assert client.get("/api/cycles").status_code == 401


@pytest.mark.parametrize("identity", list(IDENTITY_PROFILES))
def test_authenticated_plan_reads_return_stored_content(identity):
    from unified_agent.tools.plan import plan_create

    plan_id = plan_create("authenticated read fixture", ["inspect"])["plan_id"]
    client = _client(routes.web_routes())
    listed = client.get("/api/plans", headers=_bearer(identity))
    assert listed.status_code == 200, listed.text
    assert plan_id in [plan["id"] for plan in listed.json()["plans"]]
    detail = client.get(f"/api/plans/{plan_id}", headers=_bearer(identity))
    assert detail.status_code == 200, detail.text
    assert detail.json()["plan"]["id"] == plan_id
    assert client.get("/api/plans").status_code == 401


def test_operator_shipped_workflow_runs_selected_step():
    client = _client(routes.web_routes())
    response = client.post(
        "/api/action/workflow",
        headers=_bearer(OPERATOR),
        json={"name": "audit-triage-file-tasks"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done", response.text
    assert response.json()["steps"][0]["tool"] == "audit_triage_tasks"
    assert response.json()["outputs"]["triage"]["created"] == 0
    token = config.agent_identity_var.set(OPERATOR)
    try:
        assert not profiles.tool_is_allowed("audit_triage_tasks")
        assert not profiles.tool_is_allowed("workflow_run")
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize(
    "action,status", [("deny", "denied"), ("require_approval", "pending-approval")]
)
def test_operator_workflow_keeps_nested_policy_gate(action, status, monkeypatch, tmp_path):
    from unified_agent.tools import approvals

    effects = []

    def audit_triage_tasks(min_severity="high"):
        effects.append(min_severity)
        return {"created": 0}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "audit_triage_tasks",
        server._guarded_and_instrumented(audit_triage_tasks),
    )
    policy_file = tmp_path / "workflow-policy.yaml"
    policy_file.write_text(
        "rules:\n  - name: gate_nested_triage\n    type: tool\n"
        f"    tool: audit_triage_tasks\n    action: {action}\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    response = _client(routes.web_routes()).post(
        "/api/action/workflow",
        headers=_bearer(OPERATOR),
        json={"name": "audit-triage-file-tasks"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status, response.text
    assert effects == []
    pending = approvals.approval_pending()["pending"]
    if action == "require_approval":
        assert len(pending) == 1 and pending[0]["tool"] == "audit_triage_tasks"
    else:
        assert pending == []
        assert "gate_nested_triage" in response.json()["steps"][0]["error"]


def test_shared_read_declarations_reject_mutations_and_unknown_capabilities(monkeypatch):
    for kwargs in (
        {"read_tools": ("task_update",)},
        {"read_actions": (("plan_manage", "plan_review"),)},
    ):
        with pytest.raises(ValueError, match="unknown shared read capability"):
            Scope(Access.ANY_AUTHENTICATED, **kwargs)
    with pytest.raises(ValueError, match="non-mutating authenticated"):
        Scope(Access.ANY_AUTHENTICATED, csrf=True, read_tools=("autonomous_cycle_get",))
    with pytest.raises(ValueError, match="non-mutating authenticated"):
        Scope(Access.PUBLIC, read_tools=("autonomous_cycle_get",))
    scope = Scope(Access.ANY_AUTHENTICATED, read_actions=(("plan_manage", "plan_get"),))
    monkeypatch.setitem(ROUTE_SCOPES, ("POST", "/api/plans"), scope)
    effects = []

    async def handler(request):
        effects.append("invoked")
        return JSONResponse({})

    response = _client([routes._scoped(Route("/api/plans", handler, methods=["POST"]))]).post(
        "/api/plans", headers=_bearer(OPERATOR), json={}
    )
    assert response.status_code == 403
    assert effects == []


def test_shared_plan_read_grants_only_the_selected_read_action():
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.webui import api

    async def handler(request):
        assert profiles.consolidated_action_is_allowed("plan_manage", "plan_get")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_list")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_review")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_create")
        with pytest.raises(ToolError, match="not exposed"):
            await api.call_tool(
                "plan_manage", action="create", params={"goal": "forbidden", "steps": ["x"]}
            )
        return JSONResponse({"ok": True})

    response = _client(
        [routes._scoped(Route("/api/plans/{plan_id}", handler, methods=["GET"]))]
    ).get("/api/plans/fixture", headers=_bearer(OPERATOR))
    assert response.status_code == 200, response.text
    token = config.agent_identity_var.set(OPERATOR)
    try:
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_get")
    finally:
        config.agent_identity_var.reset(token)


def test_http_workflow_step_cannot_invoke_unselected_tool_or_change_identity(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    effects = []

    def audit_triage_tasks(min_severity="high"):
        assert profiles.tool_is_allowed("audit_triage_tasks")
        assert not profiles.tool_is_allowed("workflow_run")
        assert not profiles.tool_is_allowed("task_update")
        assert "audit_triage_tasks" not in profiles.allowed_tools(server.REGISTERED_TOOLS)
        with pytest.raises(ToolError, match="not exposed"):
            server.REGISTERED_TOOLS["task_update"](id=9999, title="forbidden")
        token = config.agent_identity_var.set("server_agent8")
        try:
            assert not profiles.tool_is_allowed("audit_triage_tasks")
        finally:
            config.agent_identity_var.reset(token)
        effects.append(min_severity)
        return {"created": 0}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "audit_triage_tasks",
        server._guarded_and_instrumented(audit_triage_tasks),
    )
    response = _client(routes.web_routes()).post(
        "/api/action/workflow", headers=_bearer(OPERATOR), json={"name": "audit-triage-file-tasks"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done", response.text
    assert effects == ["high"]
    token = config.agent_identity_var.set(OPERATOR)
    try:
        with pytest.raises(ToolError, match="not exposed"):
            # No HTTP authority: the same guarded step still follows the MCP ceiling.
            server.REGISTERED_TOOLS["audit_triage_tasks"]()
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("read_tool", ["autonomous_cycle_list", "plan_manage"])
def test_shared_read_authority_keeps_policy_denials(read_tool, monkeypatch, tmp_path):
    policy_file = tmp_path / "read-policy.yaml"
    policy_file.write_text(
        "rules:\n  - name: no_shared_read\n    type: tool\n"
        f"    tool: {read_tool}\n    action: deny\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    path = "/api/cycles" if read_tool == "autonomous_cycle_list" else "/api/plans/fixture"
    response = _client(routes.web_routes()).get(path, headers=_bearer(OPERATOR))
    assert "no_shared_read" in response.text


def test_http_workflow_step_keeps_agent_access_gate(monkeypatch):
    from unified_agent import agent_access
    from unified_agent.agent_access import AgentAccessDenied

    effects = []
    original = agent_access.enforce

    def enforce(operation, **kwargs):
        if operation == "audit_triage_tasks":
            raise AgentAccessDenied("nested audit access denied")
        return original(operation, **kwargs)

    def audit_triage_tasks(min_severity="high"):
        effects.append(min_severity)
        return {"created": 0}

    monkeypatch.setattr(agent_access, "enforce", enforce)
    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "audit_triage_tasks",
        server._guarded_and_instrumented(audit_triage_tasks),
    )
    response = _client(routes.web_routes()).post(
        "/api/action/workflow", headers=_bearer(OPERATOR), json={"name": "audit-triage-file-tasks"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "denied"
    assert "nested audit access denied" in response.json()["steps"][0]["error"]
    assert effects == []


def test_expected_authenticated_browser_reads_do_not_emit_missing_mcp_warning(caplog):
    client = _client(routes.web_routes())
    with caplog.at_level("WARNING", logger="unified_agent.profiles"):
        for _ in range(2):
            response = client.get("/api/tasks", headers=_bearer("Desktop_Agent2"))
            assert response.status_code == 200, response.text
    assert not any("has no MCP session id" in record.getMessage() for record in caplog.records)


def test_operator_post_merge_workflow_reaches_restricted_guarded_stub_steps(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    effects = []

    def run_cmd(command, timeout_s=120):
        effects.append(("run_cmd", command, timeout_s))
        return {"stdout": "isolated fixture", "stderr": "", "returncode": 0}

    def security_audit(remediate=False):
        assert remediate is False
        effects.append(("security_audit",))
        return {"status": "pass"}

    def system_doctor(refresh: bool = False):
        # post-merge-verify must force a fresh run, not read the shared report
        # (task #1897).
        assert refresh is True
        effects.append(("system_doctor",))
        return {"checks": [], "summary": {}}

    for fn in (run_cmd, security_audit, system_doctor):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS, fn.__name__, server._guarded_and_instrumented(fn)
        )
    response = _client(routes.web_routes()).post(
        "/api/action/workflow", headers=_bearer(OPERATOR), json={"name": "post-merge-verify"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done", response.text
    assert effects[0] == ("run_cmd", "pytest -q -p no:cacheprovider -m smoke", 120)
    assert [effect[0] for effect in effects] == ["run_cmd", "security_audit", "system_doctor"]
    token = config.agent_identity_var.set(OPERATOR)
    try:
        assert not profiles.tool_is_allowed("run_cmd")
        with pytest.raises(ToolError, match="not exposed"):
            server.REGISTERED_TOOLS["run_cmd"]("unused fixture command")
    finally:
        config.agent_identity_var.reset(token)
    assert len(effects) == 3  # Every body is a stub; no subprocess or actual audit runs.


@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
def test_workflow_step_respects_nonoperator_explicit_deny(identity, monkeypatch):
    from unified_agent.tools import tasks

    task = tasks.task_add("private review fixture")
    effects = []

    def review_and_notify(task_id: int):
        effects.append(task_id)
        return {"status": "fixture-only"}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS,
        "review_and_notify",
        server._guarded_and_instrumented(review_and_notify),
    )
    response = _client(routes.web_routes()).post(
        "/api/action/workflow",
        headers=_bearer(identity),
        json={"name": "review-and-PR", "inputs": {"task_id": task["id"]}},
    )
    assert response.status_code == 200, response.text
    if identity == OPERATOR:
        assert response.json()["status"] == "done", response.text
        assert effects == [task["id"]]
    else:
        assert response.json()["status"] == "denied", response.text
        assert effects == []
