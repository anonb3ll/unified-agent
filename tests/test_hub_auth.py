import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, mcp_runtime_telemetry, server
from unified_agent.server import HubAuthASGIMiddleware


# A simple endpoint that returns the verified identity and current workspace
async def homepage(request):
    identity = config.verified_agent_identity()
    return JSONResponse({"identity": identity, "workspace": str(config.workspace_root())})


@pytest.fixture
def auth_app():
    # Not "/": exact "/" is public and redirects in the real hub (task #1400),
    # which would make every auth assertion below moot.
    app = Starlette(routes=[Route("/probe", homepage, methods=["GET", "POST"])])
    return HubAuthASGIMiddleware(app)


class TestHubAuth:
    def test_no_tokens_configured_passes_through(self, auth_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        client = TestClient(auth_app)
        resp = client.get("/probe")
        assert resp.status_code == 200
        assert resp.json()["identity"] is None

    def test_valid_token_authenticates(self, auth_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123,AgentB:token456")
        client = TestClient(auth_app)

        # Mismatch/missing
        resp = client.get("/probe")
        assert resp.status_code == 200  # Default warn-only mode doesn't reject
        assert resp.json()["identity"] is None

        # Valid
        resp = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp.status_code == 200
        assert resp.json()["identity"] == "AgentA"

    def test_enforce_mode_rejects_unauthorized(self, auth_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        client = TestClient(auth_app)

        # Missing token
        resp = client.get("/probe")
        assert resp.status_code == 401
        assert "Unauthorized" in resp.json()["error"]

        # Invalid token
        resp = client.get("/probe", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401

        # Valid token
        resp = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp.status_code == 200
        assert resp.json()["identity"] == "AgentA"

    def test_no_workspace_mapping_falls_back_to_env_var(self, auth_app, monkeypatch):
        """An authenticated agent with no UA_AGENT_WORKSPACES entry keeps today's behavior."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/default-ws")
        client = TestClient(auth_app)

        resp = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp.status_code == 200
        assert resp.json()["identity"] == "AgentA"
        assert resp.json()["workspace"] == "/tmp/default-ws"

    def test_mapped_agent_gets_its_own_workspace(self, auth_app, monkeypatch):
        """A verified agent with a UA_AGENT_WORKSPACES entry gets that root, not the process default."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123,AgentB:token456")
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES", "AgentA:/tmp/agent-a-workspace"
        )  # AgentB deliberately unmapped
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/default-ws")
        client = TestClient(auth_app)

        resp_a = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp_a.json() == {"identity": "AgentA", "workspace": "/tmp/agent-a-workspace"}

        # AgentB is verified but has no mapping — falls back to the shared default,
        # proving one agent's binding doesn't leak into another's request.
        resp_b = client.get("/probe", headers={"Authorization": "Bearer token456"})
        assert resp_b.json() == {"identity": "AgentB", "workspace": "/tmp/default-ws"}

        # Unauthenticated request in between is unaffected by either binding
        resp_none = client.get("/probe")
        assert resp_none.json() == {"identity": None, "workspace": "/tmp/default-ws"}

    def test_workspace_lookup_failure_does_not_leak_identity_contextvar(
        self, auth_app, monkeypatch
    ):
        """If resolving the workspace mapping raises, no ContextVar should be left set.

        Regression guard for the fix that moved the agent_workspaces() lookup before
        any .set() call: previously an exception there would skip the try/finally
        reset entirely, since it ran before the try block was even entered.
        """
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")

        def boom():
            raise RuntimeError("simulated malformed UA_AGENT_WORKSPACES")

        monkeypatch.setattr(config, "agent_workspaces", boom)
        client = TestClient(auth_app, raise_server_exceptions=False)

        resp = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp.status_code == 500  # the request fails, but cleanly

        # No ContextVar was ever set for this request, so there's nothing stale left
        # for the next one — confirmed by a subsequent unauthenticated request behaving
        # exactly as if the failing request never happened.
        assert config.agent_identity_var.get() is None
        assert config.workspace_root_var.get() is None

    def test_workspace_contextvar_reset_after_request(self, auth_app, monkeypatch):
        """The per-request override must not leak to code running outside the request."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "AgentA:/tmp/agent-a-workspace")
        monkeypatch.setenv("UA_WORKSPACE", "/tmp/default-ws")
        client = TestClient(auth_app)

        client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert config.workspace_root_var.get() is None
        from pathlib import Path

        assert config.workspace_root() == Path("/tmp/default-ws")

    def test_denied_agent_gets_no_workspace_at_all(self, auth_app, monkeypatch):
        """A UA_AGENT_WORKSPACES "DENY" entry rejects hub-mediated fs access outright —
        it must never fall through to the shared UA_WORKSPACE root the way an
        unmapped identity does. Regression guard for Laptop_* identities silently
        inheriting the desktop's own filesystem path (task #289)."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Laptop_agent3:token789")
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Laptop_agent3:DENY")
        monkeypatch.setenv("UA_WORKSPACE", "/home/you/Projects/agent")
        client = TestClient(auth_app, raise_server_exceptions=False)

        resp = client.get("/probe", headers={"Authorization": "Bearer token789"})
        # The homepage handler calls config.workspace_root() directly, which now
        # raises for a denied identity — that surfaces as a request failure, not
        # as a 200 carrying the desktop's own path.
        assert resp.status_code == 500

    def test_deny_value_parses_to_sentinel(self, monkeypatch):
        """agent_workspaces() maps a "DENY" value to WORKSPACE_DENIED, not a literal path."""
        monkeypatch.setenv("UA_AGENT_WORKSPACES", "Laptop_agent3:DENY,Laptop_Agent2:deny")
        mapping = config.agent_workspaces()
        assert mapping["Laptop_agent3"] is config.WORKSPACE_DENIED
        assert mapping["Laptop_Agent2"] is config.WORKSPACE_DENIED

    def test_workspace_root_raises_for_denied_identity(self, monkeypatch):
        """workspace_root() must reject, not redirect, once bound to WORKSPACE_DENIED."""
        from mcp.server.mcpserver.exceptions import ToolError

        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            with pytest.raises(ToolError, match="denied"):
                config.workspace_root()
        finally:
            config.workspace_root_var.reset(token)

    def test_unbound_laptop_identity_fails_closed_not_desktop_fallback(self, auth_app, monkeypatch):
        """Regression guard for task #299: an authenticated Laptop_* identity with no
        UA_AGENT_WORKSPACES entry must be denied, not silently handed the desktop's
        shared UA_WORKSPACE root the way a non-device-scoped identity is."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Laptop_Agent1:laptop-tok")
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        monkeypatch.setenv("UA_WORKSPACE", "/home/you/Projects/agent")
        client = TestClient(auth_app, raise_server_exceptions=False)

        resp = client.get("/probe", headers={"Authorization": "Bearer laptop-tok"})
        assert resp.status_code == 500  # denied, never a 200 carrying the desktop path

    def test_all_four_laptop_identities_positive_and_negative_paths(self, auth_app, monkeypatch):
        """Live-request-shaped coverage for every Laptop_* identity (task #299 acceptance):
        bound -> its own laptop-local path; unbound -> denied, never the desktop path."""
        laptop_labels = ["Laptop_Agent1", "Laptop_Agent2", "Laptop_agent3", "Laptop_Agent5"]
        tokens = {label: f"tok-{label}" for label in laptop_labels}
        monkeypatch.setenv(
            "UA_AGENT_TOKENS", ",".join(f"{label}:{tok}" for label, tok in tokens.items())
        )
        # Only two of the four get a real binding, to prove per-identity independence.
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES",
            "Laptop_Agent1:/home/laptopuser/dev/unified_agent,"
            "Laptop_Agent2:/home/laptopuser/dev/myagent",
        )
        monkeypatch.setenv("UA_WORKSPACE", "/home/you/Projects/agent")
        client = TestClient(auth_app, raise_server_exceptions=False)

        # Positive: bound identities get exactly their configured laptop-local path.
        resp = client.get("/probe", headers={"Authorization": f"Bearer {tokens['Laptop_Agent1']}"})
        assert resp.json() == {
            "identity": "Laptop_Agent1",
            "workspace": "/home/laptopuser/dev/unified_agent",
        }
        resp = client.get("/probe", headers={"Authorization": f"Bearer {tokens['Laptop_Agent2']}"})
        assert resp.json() == {
            "identity": "Laptop_Agent2",
            "workspace": "/home/laptopuser/dev/myagent",
        }

        # Negative: unbound identities are denied, and specifically never see the
        # desktop's own UA_WORKSPACE path (the exact failure mode task #299 exists to close).
        for label in ("Laptop_agent3", "Laptop_Agent5"):
            resp = client.get("/probe", headers={"Authorization": f"Bearer {tokens[label]}"})
            assert resp.status_code == 500
            assert "/home/you/Projects/agent" not in resp.text

    def test_desktop_identity_retains_shared_workspace_access(self, auth_app, monkeypatch):
        """Desktop_* identities are not device-scoped: no entry keeps the existing
        shared-fallback behavior (they get full access to desktop-local checkouts)."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:desktop-tok")
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        monkeypatch.setenv("UA_WORKSPACE", "/home/you/Projects/agent")
        client = TestClient(auth_app)

        resp = client.get("/probe", headers={"Authorization": "Bearer desktop-tok"})
        assert resp.json() == {
            "identity": "Desktop_Agent1",
            "workspace": "/home/you/Projects/agent",
        }

    def test_stale_duplicate_workspace_registration_fails_closed_end_to_end(
        self, auth_app, monkeypatch
    ):
        """A duplicate UA_AGENT_WORKSPACES entry for one label (e.g. an old DENY left
        in place alongside a newer real path) must deny the live request, not pick
        whichever entry happens to parse last."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Laptop_agent3:tok-agy")
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES",
            "Laptop_agent3:DENY,Laptop_agent3:/home/laptopuser/dev/myagent-brain",
        )
        client = TestClient(auth_app, raise_server_exceptions=False)

        resp = client.get("/probe", headers={"Authorization": "Bearer tok-agy"})
        assert resp.status_code == 500

    def test_swapped_token_does_not_grant_the_other_identitys_workspace(
        self, auth_app, monkeypatch
    ):
        """A caller presenting AgentB's token can never end up bound to AgentA's
        workspace, or vice versa — the hub trusts only the verified token, never
        any client-supplied label."""
        monkeypatch.setenv("UA_AGENT_TOKENS", "Laptop_Agent1:tok-a,Desktop_Agent1:tok-b")
        monkeypatch.setenv(
            "UA_AGENT_WORKSPACES", "Laptop_Agent1:/home/laptopuser/dev/unified_agent"
        )
        monkeypatch.setenv("UA_WORKSPACE", "/home/you/Projects/agent")
        client = TestClient(auth_app, raise_server_exceptions=False)

        # tok-b (Desktop_Agent1's real token) must resolve to Desktop_Agent1's own
        # binding, never to Laptop_Agent1's laptop-local path.
        resp = client.get("/probe", headers={"Authorization": "Bearer tok-b"})
        assert resp.json() == {
            "identity": "Desktop_Agent1",
            "workspace": "/home/you/Projects/agent",
        }

    def test_verify_write_identity_mismatch(self, monkeypatch):
        # Setup context
        token = config.agent_identity_var.set("AgentA")
        try:
            # Matches
            assert config.verify_write_identity("AgentA") == "AgentA"
            # None defaults to verified
            assert config.verify_write_identity(None) == "AgentA"

            # Mismatch in warn-only
            monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
            assert (
                config.verify_write_identity("AgentB") == "AgentA"
            )  # Warn only returns verified to stamp

            # Mismatch in enforce mode raises ToolError
            monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
            from mcp.server.mcpserver.exceptions import ToolError

            with pytest.raises(ToolError, match="Auth identity mismatch"):
                config.verify_write_identity("AgentB")
        finally:
            config.agent_identity_var.reset(token)

    def test_verify_write_identity_allowlisted_alias(self, monkeypatch):
        """Desktop_Person (task #365): a narrow, explicit (verified, claimed)
        pair may stand in for each other — everything else still mismatches."""
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            # Allowlisted pair succeeds even in enforce mode — the allowlist
            # check runs before the warn/raise branch, not instead of it.
            monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
            assert config.verify_write_identity("Operator") == "Operator"

            # A claim not on the allowlist for this verified identity still
            # mismatches exactly as before, enforce mode included.
            from mcp.server.mcpserver.exceptions import ToolError

            with pytest.raises(ToolError, match="Auth identity mismatch"):
                config.verify_write_identity("Desktop_Agent1")
        finally:
            config.agent_identity_var.reset(token)

        # The allowlist is keyed by verified identity — "Operator" claiming to
        # be "Desktop_Person" is not the reverse-permitted direction.
        token = config.agent_identity_var.set("Operator")
        try:
            monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
            assert config.verify_write_identity("Desktop_Person") == "Operator"
        finally:
            config.agent_identity_var.reset(token)


# --- public-host route allowlist (task #859: restricted Grok Bot slice) ------


async def _echo(request):
    return JSONResponse({"path": request.url.path})


@pytest.fixture
def sliced_app():
    routes = [
        Route("/mcp", _echo, methods=["GET", "POST"]),
        Route("/ui", _echo),
        Route("/api/board", _echo),
        Route("/healthz", _echo),
    ]
    return HubAuthASGIMiddleware(Starlette(routes=routes))


class TestPublicHostSlice:
    def test_public_host_reaches_mcp_only(self, sliced_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "grok.example.ts.net")
        client = TestClient(sliced_app)
        pub = {"host": "grok.example.ts.net"}

        assert client.get("/mcp", headers=pub).status_code == 200
        for blocked in ("/ui", "/api/board", "/healthz"):
            resp = client.get(blocked, headers=pub)
            assert resp.status_code == 404, blocked
            assert resp.json()["error"] == "not found"

    def test_public_host_match_ignores_port(self, sliced_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "grok.example.ts.net")
        client = TestClient(sliced_app)
        assert client.get("/ui", headers={"host": "grok.example.ts.net:443"}).status_code == 404

    def test_non_public_host_is_unaffected(self, sliced_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "grok.example.ts.net")
        client = TestClient(sliced_app)
        for path in ("/ui", "/api/board", "/healthz", "/mcp"):
            assert client.get(path, headers={"host": "hub.internal.ts.net"}).status_code == 200, (
                path
            )

    def test_unset_public_hosts_is_a_noop(self, sliced_app, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.delenv("UA_HTTP_PUBLIC_HOSTS", raising=False)
        client = TestClient(sliced_app)
        assert client.get("/ui", headers={"host": "anything"}).status_code == 200


class TestWorkOSLeg:
    """The optional WorkOS AuthKit credential leg (task #1202)."""

    @pytest.fixture
    def leg(self, monkeypatch):
        from unified_agent import workos_hub_auth

        class Leg:
            def __init__(self):
                self.calls = 0
                self.session = {
                    "identity": "Desktop_Agent1",
                    "subject": "user-a",
                    "organization_id": "org-a",
                    "session_id": "session-a",
                    "expires_at": 4102444800,
                }

            def resolve(self, sealed):
                self.calls += 1
                return self.session if sealed == "good-session" else None

        instance = Leg()
        monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: instance)
        monkeypatch.setattr(workos_hub_auth, "enabled", lambda: True)
        return instance

    def test_sealed_session_binds_the_mapped_human_identity(self, auth_app, leg, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        client = TestClient(auth_app)
        resp = client.get("/probe", headers={"Authorization": "WorkOS good-session"})
        assert resp.status_code == 200
        assert resp.json()["identity"] == "Desktop_Agent1"

    def test_middleware_binds_workos_through_bind_workos_request_auth(self, leg, monkeypatch):
        """HubAuthASGIMiddleware must bind the session through the helper.

        Rebuilding the auth_metadata dict inline and calling
        mark_workos_request_bound() from the middleware fails this test: the
        sentinel's caller is bind_workos_request_auth, and the handler sees
        that helper's dict object.
        """
        import inspect

        captured: dict = {}
        bind_calls: list[dict] = []
        bound_meta: list = []
        mark_callers: list[str] = []
        real_bind = config.bind_workos_request_auth
        real_mark = config.mark_workos_request_bound

        def spy_bind(**kwargs):
            bind_calls.append(kwargs)
            tokens = real_bind(**kwargs)
            bound_meta.append(config.current_auth_metadata())
            return tokens

        def spy_mark():
            mark_callers.append(inspect.stack()[1].function)
            return real_mark()

        monkeypatch.setattr(config, "bind_workos_request_auth", spy_bind)
        monkeypatch.setattr(config, "mark_workos_request_bound", spy_mark)

        async def probe(_request):
            captured["meta"] = config.current_auth_metadata()
            captured["bound"] = config.workos_request_is_bound()
            captured["identity"] = config.verified_agent_identity()
            return JSONResponse({"ok": True})

        app = HubAuthASGIMiddleware(Starlette(routes=[Route("/", probe)]))
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        resp = TestClient(app).get("/", headers={"Authorization": "WorkOS good-session"})

        assert resp.status_code == 200
        assert captured["identity"] == "Desktop_Agent1"
        assert captured["bound"] is True
        assert bind_calls == [
            {
                "identity": "Desktop_Agent1",
                "subject": "user-a",
                "organization_id": "org-a",
                "session_id": "session-a",
                "expires_at": 4102444800,
            }
        ]
        assert mark_callers == ["bind_workos_request_auth"]
        assert captured["meta"] is bound_meta[0]
        assert captured["meta"] == {
            "auth_method": "workos",
            "scopes": [],
            "workos_user_id": "user-a",
            "workos_organization_id": "org-a",
            "workos_session_id": "session-a",
            "exp": 4102444800,
        }

    def test_rejected_session_stays_anonymous_and_is_refused_under_enforcement(
        self, auth_app, leg, monkeypatch
    ):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        client = TestClient(auth_app)
        assert client.get("/probe", headers={"Authorization": "WorkOS revoked"}).status_code == 401

    def test_the_leg_alone_authenticates_the_hub_with_no_tokens_configured(
        self, auth_app, leg, monkeypatch
    ):
        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        client = TestClient(auth_app)
        resp = client.get("/probe", headers={"Authorization": "WorkOS good-session"})
        assert resp.json()["identity"] == "Desktop_Agent1"

    def test_a_bearer_token_still_wins_and_never_reaches_the_leg(self, auth_app, leg, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        client = TestClient(auth_app)
        resp = client.get("/probe", headers={"Authorization": "Bearer token123"})
        assert resp.json()["identity"] == "AgentA"
        assert leg.calls == 0

    def test_the_session_is_revalidated_on_every_request(self, auth_app, leg, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        client = TestClient(auth_app)
        for _ in range(3):
            client.get("/probe", headers={"Authorization": "WorkOS good-session"})
        assert leg.calls == 3

    def test_disabled_leg_leaves_the_header_inert(self, auth_app, monkeypatch):
        from unified_agent import workos_hub_auth

        monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: None)
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        client = TestClient(auth_app)
        assert (
            client.get("/probe", headers={"Authorization": "WorkOS good-session"}).status_code
            == 401
        )

    def test_a_mapping_to_an_unknown_label_is_refused_at_the_middleware_too(
        self, auth_app, leg, monkeypatch
    ):
        leg.session = dict(leg.session, identity="not a label!")
        monkeypatch.setenv("UA_AGENT_TOKENS", "AgentA:token123")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        client = TestClient(auth_app)
        assert (
            client.get("/probe", headers={"Authorization": "WorkOS good-session"}).status_code
            == 401
        )

    def test_a_leg_that_fails_to_build_does_not_open_the_hub(self, auth_app, monkeypatch):
        """A configured-but-unbuildable leg must fail closed, not fall through.

        With no bearer tokens and no OAuth provider, the middleware's fast path
        serves the request anonymously without consulting UA_ENFORCE_AUTH. An
        operator whose only credential leg is WorkOS must not land there just
        because the leg could not be constructed (Devin review, PR #677).
        """
        from unified_agent import workos_hub_auth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setattr(workos_hub_auth, "enabled", lambda: True)
        monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: None)
        client = TestClient(auth_app)
        assert client.get("/probe").status_code == 401
        assert client.get("/probe", headers={"Authorization": "WorkOS anything"}).status_code == 401

    def test_an_unconfigured_leg_is_refused_when_auth_is_enforced(self, auth_app, monkeypatch):
        """No bearer, OAuth, or WorkOS leg must not serve anonymously under enforcement.

        The fast path used to call the app without consulting UA_ENFORCE_AUTH, so
        a missing token file (parsed as {}) returned 200 with identity None.
        """
        from unified_agent import workos_hub_auth

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setattr(workos_hub_auth, "enabled", lambda: False)
        monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: None)
        client = TestClient(auth_app)
        # / is deliberately public for the UI redirect; use a gated route to
        # preserve this fail-closed check when no auth leg is configured.
        assert client.get("/probe").status_code == 401


# --- request context cleanup (task #2219) ----------------------------------------
# Request context must unwind in the caller's task, including telemetry failures.


_REQUEST_VARS = (
    config.agent_identity_var,
    config.auth_metadata_var,
    config.peer_device_var,
    config.mcp_session_id_var,
    config.workspace_root_var,
    config._workos_bound_var,
)


def _context():
    return tuple(var.get() for var in _REQUEST_VARS)


@pytest.mark.parametrize("credential", ["bearer", "workos"])
@pytest.mark.parametrize("outer_context", [False, True], ids=["empty", "nested"])
@pytest.mark.parametrize("outcome", ["success", "telemetry_error", "app_error", "cancelled"])
async def test_request_context_restored_in_same_task(
    monkeypatch, tmp_path, credential, outer_context, outcome
):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:test-token,Desktop_Agent4:other-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    workspace = tmp_path / "agent-workspace"
    monkeypatch.setenv("UA_AGENT_WORKSPACES", f"Desktop_Agent2:{workspace}")
    monkeypatch.setattr(server.oauth, "get_provider", lambda: None)
    claims = {
        "identity": "Desktop_Agent2",
        "subject": "test-subject",
        "organization_id": "test-org",
        "session_id": "test-workos-session",
        "expires_at": 4102444800,
    }
    leg = SimpleNamespace(resolve=lambda sealed: claims if sealed == "test-session" else None)
    monkeypatch.setattr(server.workos_hub_auth, "hub_auth", lambda: leg)
    monkeypatch.setattr(server.workos_hub_auth, "enabled", lambda: True)
    device = {"node": "test-device"}
    monkeypatch.setattr(server.tailnet, "resolve_peer_device", lambda ip: device)
    monkeypatch.setattr(server.registry, "touch_agent", lambda *args, **kwargs: None)
    mcp_runtime_telemetry.reset_telemetry()

    # Seed and restore in this coroutine, so neither TestClient nor a fresh task
    # can mask a leak. Non-default values also catch cleanup that only sets None.
    outer = (
        (
            "outer-agent",
            {"auth_method": "outer"},
            {"node": "outer"},
            "outer-session",
            Path("/outer-workspace"),
            object(),
        )
        if outer_context
        else (None,) * len(_REQUEST_VARS)
    )
    tokens = [var.set(value) for var, value in zip(_REQUEST_VARS, outer, strict=True)]
    caller_task = asyncio.current_task()
    seen = []
    touches = []
    error = RuntimeError("injected request failure")
    real_touch = mcp_runtime_telemetry.record_session_touch

    def touch(**kwargs):
        touches.append((kwargs, _context()))
        if outcome == "telemetry_error":
            raise error
        real_touch(**kwargs)

    monkeypatch.setattr(mcp_runtime_telemetry, "record_session_touch", touch)

    async def app(scope, receive, send):
        assert asyncio.current_task() is caller_task
        seen.append(_context())
        if outcome == "app_error":
            raise error
        if outcome == "cancelled":
            caller_task.cancel()
            await asyncio.sleep(0)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail(f"Unexpected middleware response: {message['type']}")

    authorization = b"Bearer test-token" if credential == "bearer" else b"WorkOS test-session"
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": ("127.0.0.1", 12345),
        "headers": [(b"authorization", authorization), (b"mcp-session-id", b"test-mcp-session")],
    }
    try:
        middleware = server.HubAuthASGIMiddleware(app)
        if outcome == "success":
            await middleware(scope, receive, send)
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await middleware(scope, receive, send)
            caller_task.uncancel()
        else:
            with pytest.raises(RuntimeError) as caught:
                await middleware(scope, receive, send)
            assert caught.value is error

        assert asyncio.current_task() is caller_task
        assert _context() == outer
        assert len(touches) == 1
        kwargs, during_touch = touches[0]
        assert kwargs == {"session_id": "test-mcp-session", "agent_label": "Desktop_Agent2"}
        assert during_touch[:4] == (
            "Desktop_Agent2",
            {"auth_method": "bearer", "scopes": []}
            if credential == "bearer"
            else {
                "auth_method": "workos",
                "scopes": [],
                "workos_user_id": "test-subject",
                "workos_organization_id": "test-org",
                "workos_session_id": "test-workos-session",
                "exp": 4102444800,
            },
            device,
            "test-mcp-session",
        )
        assert during_touch[4] == outer[4]
        assert during_touch[5] is (config._WORKOS_BOUND if credential == "workos" else outer[5])
        if outcome == "telemetry_error":
            assert seen == []

            async def next_app(scope, receive, send):
                assert asyncio.current_task() is caller_task
                seen.append(_context())

            monkeypatch.setattr(mcp_runtime_telemetry, "record_session_touch", real_touch)
            next_middleware = server.HubAuthASGIMiddleware(next_app)
            # Subsequent anonymous (with and without a session) and other-agent
            # work runs in this same task after the failed authenticated request.
            for identity, headers, session_id in (
                (None, [], None),
                (None, [(b"mcp-session-id", b"anonymous-session")], "anonymous-session"),
                ("Desktop_Agent4", [(b"authorization", b"Bearer other-token")], None),
            ):
                await next_middleware({**scope, "headers": headers}, receive, send)
                assert _context() == outer
                assert seen[-1] == (
                    identity,
                    {"auth_method": "bearer", "scopes": []} if identity else None,
                    device if identity else None,
                    session_id,
                    outer[4],
                    outer[5],
                )
        else:
            assert seen == [(*during_touch[:4], workspace, during_touch[5])]
            snapshot = mcp_runtime_telemetry.get_telemetry_snapshot()
            assert snapshot["session"]["last_id"] == "test-mcp-session"
            assert snapshot["session"]["last_agent"] == "Desktop_Agent2"
            assert snapshot["session"]["touches_count"] == 1
    finally:
        # Keep even a deliberately base-failing run from contaminating teardown.
        for var, token in reversed(list(zip(_REQUEST_VARS, tokens, strict=True))):
            var.reset(token)
        mcp_runtime_telemetry.reset_telemetry()
