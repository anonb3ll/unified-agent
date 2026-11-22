"""Route contract for the web control center.

The load-bearing test in this file is `test_no_write_methods_on_read_routes`. M1
promises a read-only surface; that promise is worth nothing if it is only a convention,
so it is asserted for every registered route. M2's control endpoints therefore cannot
arrive unnoticed — adding one turns this red until it is deliberately updated.
"""

import secrets

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import doctor
from unified_agent.webui import api as webui_api
from unified_agent.webui import spa as web_spa

# Generated per run, not a literal: a fixed credential-shaped string in source
# trips the secret scanner, and rightly so — the scanner cannot tell a fixture
# from the real thing, and that is the property that makes it useful.
TOKEN = f"tok-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _hermetic_executor_service_probe(monkeypatch):
    """Keep attention/digest clean-system tests independent of host systemd.

    e278359 folded executor health into attention_payload and digest_payload.
    health_payload calls system_doctor, which probes unified-agent-executor.service
    via systemctl --user. On hosts where the unit is installed but inactive
    (normal after Mac-canonical cutover on a remote desktop), doctor reports
    MISCONFIGURED and the UI correctly emits executor_degraded — but tests that
    assert a clean system must not depend on that topology. Tests that *want*
    executor degradation stub system_doctor or health_payload themselves.
    """
    real_probe = doctor._service_probe
    executor_unit = doctor._SERVICE_UNITS["active_executor_service"]

    def _probe(unit: str, timeout: float):
        if unit == executor_unit:
            return {"LoadState": "not-found"}
        return real_probe(unit, timeout)

    monkeypatch.setattr(doctor, "_service_probe", _probe)
    webui_api._doctor_cache = None
    yield
    webui_api._doctor_cache = None


READ_ROUTES = [
    "/api/overview",
    "/api/attention",
    "/api/digest",
    "/api/agents",
    "/api/org",
    "/api/skills-teams",
    "/api/dispatch-routes",
    "/api/sessions",
    "/api/sessions/example/crew",
    "/api/board",
    "/api/queue-health",
    "/api/projects",
    "/api/review-approval",
    "/api/executor-delegation",
    "/api/approvals",
    "/api/memory",
    "/api/vault/search?q=test",
    "/api/vault/page?path=Wiki%2FIndex.md",
    "/api/vault/ingest-status?path=00%20-%20Inbox%2Fx.md",
    "/api/vault/briefing",
    "/api/agent-graph",
    "/api/zulip",
    "/api/execution-graph",
    "/api/dispatch-stack?task_ids=1",
    "/api/cycles",
    "/api/cycles/example",
    "/api/plans",
    "/api/plans/example",
    "/api/health",
    "/api/devices",
    "/api/nodes",
    "/api/nodes/native-sessions",
    "/api/harness-config",
    "/api/mobile/v1/projection",
    "/api/perf",
    "/api/events",
    "/api/telemetry",
    "/api/platform/manifest",
    "/api/platform/objects/board",
    "/api/platform/objects/task/1",
    "/api/whoami",
    "/api/operating",
    "/api/operating/resolve",
    "/api/operating/sprints",
    "/api/tool-allowance",
]


class TestStatic:
    def test_healthz_is_content_free(self, client):
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.json() == {"ok": True}

    @staticmethod
    def _fake_dist(tmp_path, monkeypatch):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text(
            "<html><body>modern control center</body></html>", encoding="utf-8"
        )
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)

    def test_index_is_served(self, client, tmp_path, monkeypatch):
        """/ui serves the built SPA shell — forced regardless of whether a Vite
        build happens to exist on this host."""
        self._fake_dist(tmp_path, monkeypatch)
        res = client.get("/ui")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "control center" in res.text.lower()

    def test_installed_hub_uses_configured_dist_path(self, client, tmp_path, monkeypatch):
        dist = tmp_path / "built-web"
        dist.mkdir()
        (dist / "index.html").write_text("<html>installed control center</html>")
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", tmp_path / "missing-repo-dist")
        monkeypatch.setenv("UA_WEB_DIST_DIR", str(dist))
        assert web_spa.modern_dist_ready()
        assert "installed control center" in client.get("/ui").text

    def test_missing_dist_is_503_with_build_hint(self, client, tmp_path, monkeypatch):
        """Single mount (#993): no legacy fallback — a hub without the Vite
        build gets a hard 503 naming the fix."""
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", tmp_path / "no-dist-here")
        res = client.get("/ui")
        assert res.status_code == 503
        assert "packages/web" in res.text

    def test_retired_legacy_and_excalidraw_routes_404(self, client, tmp_path, monkeypatch):
        """#993: the dual-mount is gone — every legacy/vendored path 404s."""
        self._fake_dist(tmp_path, monkeypatch)
        for path in (
            "/ui/legacy",
            "/ui/legacy/app.js",
            "/ui/legacy/excalidraw/bundle.js",
            "/ui/excalidraw/bundle.js",
        ):
            assert client.get(path).status_code == 404, path

    @pytest.mark.parametrize(
        "path",
        [
            "/ui/../../../etc/passwd",
            "/ui/%2e%2e/app.js",
            "/ui/..%2fapp.js",
            "/ui/routes.py",
            "/ui/__init__.py",
            "/ui/nonexistent.js",
        ],
    )
    def test_nothing_outside_dist_is_reachable(self, client, path, tmp_path, monkeypatch):
        """serve_modern resolves under dist only — nothing outside it can be
        served, traversal or not, and traversal never hits the SPA fallback."""
        self._fake_dist(tmp_path, monkeypatch)
        assert client.get(path).status_code == 404


class TestReadOnlyInvariant:
    @pytest.mark.parametrize("path", READ_ROUTES)
    @pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
    def test_no_write_methods_on_read_routes(self, client, path, method):
        assert getattr(client, method)(path).status_code == 405

    def test_session_route_is_the_only_mutating_endpoint(self):
        """Guard against a control route being added without updating this file."""
        mutating = set()
        for route in webui.web_routes():
            methods = set(route.methods or ()) - {"GET", "HEAD", "OPTIONS"}
            if methods:
                mutating.add(route.path)
        assert mutating == {
            "/api/session",
            "/api/slack/events",
            "/api/action/approval",
            "/api/action/config-revision",
            "/api/action/operating-sprint",
            "/api/action/operating-run",
            "/api/action/skills-teams",
            "/api/action/team-up",
            "/api/approvals/request",
            "/api/approvals/ledger-rerequest",
            "/api/approvals/ledger-poll",
            "/api/review/reads",
            "/api/tasks",
            "/api/tasks/next-work-projection",
            "/api/tasks/holds",
            "/api/tasks/holds/{hold_id}/release",
            "/api/tasks/{task_id}/claim-release",
            "/api/goals",
            "/api/tasks/{task_id}/comments",
            "/api/work-products",
            "/api/work-products/{id}/release",
            "/api/connectors/oauth/{provider}/begin",
            "/api/connectors/oauth/{provider}/disconnect",
            "/api/evidence",
            "/api/action/task",
            "/api/agent-lifecycle",
            "/api/action/workflow",
            "/api/action/dispatch",
            "/api/action/dispatch-stack",
            "/api/action/dependency",
            "/api/action/handoff",
            "/api/action/crew-intervene",
            "/api/action/node-session",
            "/api/action/node-acceptance",
            "/api/action/node-retire",
            "/api/action/work-proposal-decide",
            "/api/action/node-followup",
            "/api/action/node-resume",
            "/api/action/node-adopt",
            "/api/action/cycle",
            "/api/action/plan",
            "/api/action/proposal",
            "/api/action/memory/save",
            "/api/action/memory/delete",
            "/api/action/memory/restore",
            "/api/action/memory/promote",
            "/api/action/memory/feedback",
            "/api/action/vault/write",
            "/api/action/vault/journal",
            "/api/action/plan/create",
            "/api/action/plan/update",
            "/api/action/plan/checkpoint",
            "/api/action/plan/resume",
            "/api/action/plan/retry",
            "/api/action/plan/claim",
            "/api/action/plan/handoff",
            "/api/action/plan/close",
            "/api/action/plan/delete",
            "/api/action/plan/joint/bind",
            "/api/action/plan/joint/propose",
            "/api/action/plan/joint/review",
            "/api/action/plan/joint/decide",
            "/api/action/plan/joint/recover",
            "/api/action/task/claim",
            "/api/action/task/delete",
            "/api/action/session/end",
            "/api/action/plan/step-add",
            "/api/action/plan/step-edit",
            "/api/action/plan/step-remove",
            "/api/action/plan/reorder",
            "/api/action/plan/edit",
            "/api/action/goal/edit",
            "/api/action/goal/link",
            "/api/action/goal/unlink",
            "/api/action/goal/close",
            "/api/action/goal/reopen",
            "/api/action/session/operator-end",
            "/api/action/routine/add",
            "/api/action/routine/pause",
            "/api/action/routine/resume",
            "/api/action/routine/run",
            "/api/action/langgraph/run",
            "/api/dispatch-runs/{run_id}/settle-spawn",
            "/api/action/agent/add",
            "/api/action/agent/remove",
            "/api/action/connector/register",
            "/api/action/connector/register-package",
            "/api/action/connector/authorize",
            "/api/action/connector/revoke",
            "/api/action/org-proposal",
            "/api/action/fleet",
            "/api/action/oauth-revoke",
            "/api/action/session-credential-revoke",
            "/api/action/harness-config",
            "/api/routines/{routine_id}/fire",
            "/api/oauth/passkeys",
            "/api/oauth/passkeys/options",
            "/api/oauth/passkeys/delete",
            "/api/mobile/v1/action",
            "/api/graph-canvas",
            "/api/session-capture",
            "/api/inbox/session",
            "/api/inbox/session/owed",
            "/api/sessions/{id}/wake-grant",
            "/api/sessions/{id}/wake-decline",
            "/api/action/review-hold",
            "/api/messages",
            "/api/messages/conversations/{conversation_id}/handoff",
            "/api/teams/snapshots",
            "/api/teams/snapshots/{snapshot_id}/restore",
            "/api/node/register",
            "/api/node/heartbeat",
            "/api/node/claim",
            "/api/node/receipt",
            "/api/node/wake",
            "/api/node/wake/check",
            "/api/node/wake/receipt",
            "/api/node/dispatch",
            "/api/node/events",
            "/api/node/permission",
            "/api/node/session_credential",
            "/api/node/followups",
            "/api/node/config",
            "/api/llm/v1/chat/completions",
        }


class TestSessionCapture:
    def test_requires_auth(self, client):
        assert client.post("/api/session-capture", json={}).status_code == 401

    def test_happy_path(self, client, monkeypatch, tmp_path):
        transcript = tmp_path / "s.jsonl"
        transcript.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

        def fake_capture(path, cwd, project):
            assert cwd == "/home/you/dev/unified_agent"
            return {"stored": True, "id": "abc123", "created": True, "hash": "deadbeef"}

        monkeypatch.setattr("unified_agent.session_capture.capture_session", fake_capture)
        res = client.post(
            "/api/session-capture",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"transcript_path": str(transcript), "cwd": "/home/you/dev/unified_agent"},
        )
        assert res.status_code == 200
        assert res.json()["id"] == "abc123"

    def test_missing_transcript_is_404(self, client):
        res = client.post(
            "/api/session-capture",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"transcript_path": "/no/such/file.jsonl", "cwd": "/tmp/x"},
        )
        assert res.status_code == 404


class TestPayloads:
    def test_next_work_projection_requires_auth_and_returns_one_read(self, client, monkeypatch):
        from unified_agent import planning_loop

        expected = {"candidates": [], "evaluated": [], "live_read_at": "2026-09-29T12:00:00+00:00"}
        manifest = {"schema_version": 1, "items": [{"task_id": 307}]}
        monkeypatch.setattr(
            planning_loop,
            "next_work_projection_payload",
            lambda body: expected if body == manifest else None,
        )
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        url = "/api/tasks/next-work-projection"
        assert client.post(url, json=manifest).status_code == 401
        assert (
            client.post(
                url,
                content="{bad",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 400
        )
        res = client.post(
            url,
            json=manifest,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 200
        assert res.json() == expected
        assert client.get(url, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405

    def test_next_work_projection_bounds_request_body(self, client, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        res = client.post(
            "/api/tasks/next-work-projection",
            content="x" * (1024 * 1024 + 1),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 413

    def test_next_work_projection_bounds_response_body(self, client, monkeypatch):
        from unified_agent import planning_loop

        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setattr(
            planning_loop,
            "next_work_projection_payload",
            lambda manifest: {"candidates": [], "evaluated": [], "title": "x" * (2 * 1024 * 1024)},
        )
        res = client.post(
            "/api/tasks/next-work-projection",
            json={"schema_version": 1, "items": []},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 413
        assert len(res.content) < 1024

    def test_next_work_projection_handles_large_graph_without_task_notes(self, client, monkeypatch):
        from unified_agent import planning_loop

        dependencies = list(range(1000, 2200))
        task = {
            "id": 307,
            "title": "Current task",
            "status": "open",
            "parent_id": None,
            "lane": "dispatch-ready",
            "claimed_by": None,
            "lock_expires_at": None,
            "notes": "private task detail",
        }
        monkeypatch.setattr(
            planning_loop,
            "_read_next_work_live_snapshot",
            lambda: (
                [task],
                {307: dependencies},
                {task_id: {"status": "done", "parent_id": None} for task_id in dependencies},
                set(),
            ),
        )
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        manifest = {
            "schema_version": 1,
            "mission_task_id": 1450,
            "items": [{"task_id": 307, "rank": 1, "hard_dependencies": dependencies[:350]}],
        }
        res = client.post(
            "/api/tasks/next-work-projection",
            json=manifest,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 200
        assert [row["task_id"] for row in res.json()["candidates"]] == [307]
        assert b"private task detail" not in res.content
        assert len(res.content) < 2 * 1024 * 1024

    def test_dispatch_returns_lifecycle_ids_without_prompt(self, client, monkeypatch):
        async def fake_dispatch(payload, from_agent):
            assert payload.agent == "Desktop_agent3"
            assert payload.project == "unified-agent"
            assert payload.prompt == "Make the pane usable"
            assert from_agent == "Desktop_Agent1"
            return {
                "status": "pending",
                "task_id": 401,
                "parent_task_id": 252,
                "delegation_id": 88,
                "session_id": None,
                "worktree": None,
                "pr": None,
                "project": "unified-agent",
            }

        monkeypatch.setattr("unified_agent.webui.api.dispatch_prompt", fake_dispatch)
        res = client.post(
            "/api/action/dispatch",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "agent": "Desktop_agent3",
                "project": "unified-agent",
                "task_id": 252,
                "prompt": "Make the pane usable",
                "context": ["task_notes", "recent_validation"],
            },
        )
        assert res.status_code == 202
        body = res.json()
        assert body["status"] == "pending"
        assert body["delegation_id"] == 88
        assert "prompt" not in body
        assert "logs" not in body

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (
                {"agent": "Desktop_agent3", "project": "unified-agent", "prompt": ""},
                "prompt is required",
            ),
            ({"agent": "???", "project": "unified-agent", "prompt": "run"}, "unknown agent"),
            (
                {
                    "agent": "Desktop_agent3",
                    "project": "unified-agent",
                    "prompt": "run",
                    "context": ["secrets"],
                },
                "unsupported",
            ),
            ({"agent": "Desktop_agent3", "prompt": "run"}, "project is required"),
            (
                {"agent": "Desktop_agent3", "project": "fake-project", "prompt": "run"},
                "unknown project",
            ),
        ],
    )
    def test_dispatch_rejects_invalid_payload(self, client, payload, message):
        res = client.post(
            "/api/action/dispatch",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=payload,
        )
        assert res.status_code == 400
        assert message in res.json()["error"]

    def test_dispatch_requires_authenticated_identity(self, client):
        res = client.post(
            "/api/action/dispatch",
            json={"agent": "Desktop_agent3", "prompt": "run"},
        )
        assert res.status_code == 401


class TestDispatchIdentityAliasing:
    """Desktop_Person (the web-UI login) is deliberately unregistered as an agent
    label (memory 92437723a6e8), but `delegate`'s from_agent must be a known label.
    Dispatch aliases that one identity to the already-registered bare "Operator"
    label for this call only — everything else still sees the raw identity
    (config.IDENTITY_ALIAS_ALLOWLIST covers the deeper anti-spoofing guard)."""

    async def test_desktop_person_dispatches_as_bare_operator_label(self, monkeypatch):
        from unified_agent.webui import api
        from unified_agent.webui.contracts import DispatchRequest

        calls = []
        delegate_kwargs = {}

        async def fake_call_tool(name, /, **kwargs):
            calls.append(name)
            if name == "delegate":
                delegate_kwargs.update(kwargs)
                return {"task_id": 1, "delegation_id": 2}
            if name == "task_update":
                return {"id": 1}
            raise AssertionError(name)

        monkeypatch.setattr(api, "call_tool", fake_call_tool)
        monkeypatch.setattr(
            api,
            "start_ui_dispatch",
            lambda **_kw: {
                "launched": True,
                "spawned": True,
                "pid": 1,
                "run_id": "run-1",
                "reason": "ok",
            },
        )
        request = DispatchRequest(
            agent="Desktop_agent3",
            prompt="do it",
            task_id=None,
            context=(),
            project="unified-agent",
        )
        await api.dispatch_prompt(request, "Desktop_Person")
        assert calls[0] == "delegate"
        assert "task_update" in calls
        assert delegate_kwargs["from_agent"] == "Operator"

    async def test_other_identities_are_not_aliased(self, monkeypatch):
        from unified_agent.webui import api
        from unified_agent.webui.contracts import DispatchRequest

        seen = {}

        async def fake_call_tool(name, /, **kwargs):
            if name == "delegate":
                seen["kwargs"] = kwargs
                return {"task_id": 1, "delegation_id": 2}
            if name == "task_update":
                return {"id": 1}
            raise AssertionError(name)

        monkeypatch.setattr(api, "call_tool", fake_call_tool)
        monkeypatch.setattr(
            api,
            "start_ui_dispatch",
            lambda **_kw: {
                "launched": True,
                "spawned": True,
                "pid": 1,
                "run_id": "run-1",
                "reason": "ok",
            },
        )
        request = DispatchRequest(
            agent="Desktop_agent3",
            prompt="do it",
            task_id=None,
            context=(),
            project="unified-agent",
        )
        await api.dispatch_prompt(request, "Desktop_Agent1")
        assert seen["kwargs"]["from_agent"] == "Desktop_Agent1"

    def test_end_to_end_desktop_person_dispatch_succeeds(self, client, monkeypatch):
        """The actual bug Operator hit: a real Desktop_Person bearer request must
        clear delegate's known-label check now, not just the mocked unit tests
        above — this exercises the auth middleware, verify_write_identity, and
        _validate_label together, the same path a real /ui request takes.
        Spawn is stubbed so pytest never execs a real agent CLI.
        """
        from unified_agent.dispatch.launch import CliProcessSpawner

        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN},Desktop_Person:operator-tok")

        def fake_spawn(self, request):
            return {
                "ok": True,
                "stub": False,
                "spawned": True,
                "pid": 4242,
                "run_id": request.get("run_id"),
                "agent_label": request.get("agent_label"),
                "cli_backend": request.get("cli_backend"),
            }

        monkeypatch.setattr(CliProcessSpawner, "spawn", fake_spawn)
        res = client.post(
            "/api/action/dispatch",
            headers={"Authorization": "Bearer operator-tok"},
            json={"agent": "Desktop_agent3", "project": "unified-agent", "prompt": "do it"},
        )
        assert res.status_code == 202, res.json()
        body = res.json()
        assert body["spawned"] is True
        assert body["pid"] == 4242
        assert body["status"] == "running"


class TestDispatchStack:
    def _stub_stack(self, monkeypatch):
        async def fake_stack(_request):
            return {
                "task_ids": [2, 1],
                "ordered_task_ids": [1, 2],
                "next_task_id": 1,
                "items": [
                    {"task_id": 1, "eligible": True, "blocked_reasons": []},
                    {
                        "task_id": 2,
                        "eligible": False,
                        "blocked_reasons": ["dependencies_incomplete"],
                    },
                ],
            }

        monkeypatch.setattr(webui_api, "dispatch_stack_payload", fake_stack)

    def test_preview_orders_dependencies_and_blocks_successor(self, client, monkeypatch):
        self._stub_stack(monkeypatch)
        response = client.get("/api/dispatch-stack?task_ids=2,1&project=unified-agent")
        assert response.status_code == 200
        body = response.json()
        assert body["ordered_task_ids"] == [1, 2]
        assert body["next_task_id"] == 1
        assert body["items"][0]["eligible"] is True
        assert body["items"][1]["blocked_reasons"] == ["dependencies_incomplete"]

    def test_preview_rejects_duplicate_task_ids(self, client):
        response = client.get("/api/dispatch-stack?task_ids=1,1")
        assert response.status_code == 400
        assert "unique" in response.json()["error"]

    def test_action_dispatches_only_next_task(self, client, monkeypatch):
        self._stub_stack(monkeypatch)
        seen = {}

        def fake_start(**kwargs):
            seen.update(kwargs)
            return {"launched": True, "run_id": "run-stack-1"}

        monkeypatch.setattr(webui_api, "start_ui_dispatch", fake_start)
        response = client.post(
            "/api/action/dispatch-stack",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"task_ids": [2, 1], "project": "unified-agent"},
        )
        assert response.status_code == 202
        assert seen["task_id"] == 1
        assert response.json()["dispatch"]["run_id"] == "run-stack-1"

    async def test_dispatch_prompt_stamps_approval_and_starts(self, monkeypatch):
        from unified_agent.tools import tasks as task_tools
        from unified_agent.webui import api
        from unified_agent.webui.contracts import DispatchRequest

        storage.init_db()
        seen = {}

        def fake_start(*, task_id, actor):
            seen["task_id"] = task_id
            seen["actor"] = actor
            return {
                "launched": True,
                "spawned": True,
                "pid": 4242,
                "run_id": "run-ui-1",
                "reason": "ok",
            }

        monkeypatch.setattr(api, "start_ui_dispatch", fake_start)
        request = DispatchRequest(
            agent="Desktop_agent3",
            prompt="do it",
            task_id=None,
            context=(),
            project="unified-agent",
        )
        out = await api.dispatch_prompt(request, "Desktop_Person")
        assert out["spawned"] is True
        assert out["status"] == "running"
        assert out["pid"] == 4242
        assert seen["actor"] == "Operator"
        assert seen["task_id"] == out["task_id"]
        task = next(
            t for t in task_tools.task_list(status="all")["tasks"] if t["id"] == out["task_id"]
        )
        assert "[plan: approved @" in (task.get("notes") or "")

    async def test_dispatch_prompt_reports_start_failure(self, monkeypatch):
        from unified_agent.webui import api
        from unified_agent.webui.contracts import DispatchRequest

        storage.init_db()

        def fake_start(*, task_id, actor):
            return {
                "launched": False,
                "spawned": False,
                "pid": None,
                "run_id": "run-fail",
                "reason": "launch_failed",
                "error": "binary_not_found:agy",
            }

        monkeypatch.setattr(api, "start_ui_dispatch", fake_start)
        request = DispatchRequest(
            agent="Desktop_agent3",
            prompt="do it",
            task_id=None,
            context=(),
            project="unified-agent",
        )
        out = await api.dispatch_prompt(request, "Desktop_Person")
        assert out["spawned"] is False
        assert out["status"] == "pending"
        assert out["error"] == "binary_not_found:agy"

    def test_agents_shape(self, client):
        body = client.get("/api/agents").json()
        assert "agents" in body and "counts" in body

    def test_org_shape(self, client):
        response = client.get("/api/org")
        assert response.status_code == 200
        offices = response.json()["offices"]
        assert offices
        assert all(
            {"id", "name", "lead", "members", "owned_epics", "open_tasks"} <= set(row)
            for row in offices
        )

    def test_sessions_shape(self, client):
        body = client.get("/api/sessions").json()
        assert set(body) >= {"sessions", "open", "total"}

    def test_projects_shape(self, client):
        body = client.get("/api/projects").json()
        assert "projects" in body
        projects = {p["project"] for p in body["projects"]}
        assert projects == {"unified-agent", "browser", "sample-store", "client-portal"}
        for entry in body["projects"]:
            assert set(entry) >= {"project", "label", "repo_path", "checked_out"}

    def test_mobile_projection_shape(self, client):
        res = client.get("/api/mobile/v1/projection")
        assert res.status_code == 200
        body = res.json()
        assert body["schema_version"] == "mobile.v1"
        assert body["fallback"]["surface"] == "iphone"
        assert body["fallback"]["deep_link_ref"] == "ua://control-center"
        assert body["health"]["safe_summary"] in {
            "Service available",
            "Service degraded",
            "Service unavailable",
            "Service status unknown",
        }
        assert body["freshness"] in {"live", "stale", "offline", "error"}

    def test_overview_composes_every_pane(self, client):
        body = client.get("/api/overview").json()
        for key in (
            "agents",
            "sessions",
            "tasks",
            "queue_health",
            "projects",
            "review_approval",
            "executor_delegation",
            "approvals",
            "anomalies",
            "zulip_worker",
            "zulip_coordination",
            "health",
            "devices",
            "auth",
            "execution_graph",
            "workflows",
        ):
            assert key in body, f"/api/overview missing {key}"
        assert set(body["auth"]) >= {"enforce", "tokens_configured", "anonymous_requests_24h"}
        assert body["zulip_worker"]["status"] == "unconfigured"
        assert body["zulip_worker"]["channels"] == []
        assert body["zulip_coordination"]["site"] == ""
        assert "UA_ZULIP_SITE" in body["zulip_coordination"]["error"]
        assert "site" in body["zulip_coordination"]
        assert "recent_inbound" in body["zulip_coordination"]
        assert "stats" in body["zulip_coordination"]

    def test_devices_shape_has_per_row_freshness(self, client):
        body = client.get("/api/devices").json()
        assert "hosts" in body
        assert body.get("error") is None
        assert body["hosts"], "hub doctor snapshot should produce at least the local host"
        for row in body["hosts"]:
            assert "host" in row
            assert "as_of" in row
            assert "staleness" in row
            assert row["doctor_freshness"] in {"live", "stale", "missing"}
            if row["doctor_freshness"] == "missing":
                assert row["doctor"] is None
                assert row["executor"]["reported"] is False
                assert row["worktrees"]["reported"] is False
            else:
                assert row["doctor"] is not None
                assert "summary" in row["doctor"]
                assert row["as_of"]

    def test_overview_workflows_are_the_definitions_that_actually_exist(self, client):
        """The Action Deck's picker offered `reconcile`, `review_pipeline` and
        `steward_audit`, none of which has ever had a YAML definition in this repo — so
        every run 400'd. Serving the real list is what stops that recurring."""
        from unified_agent.tools import workflow

        body = client.get("/api/overview").json()["workflows"]
        names = {entry["name"] for entry in body["workflows"]}
        assert names == {path.stem for path in workflow._workflows_dir().glob("*.yaml")}
        assert names, "expected at least one repo-defined workflow"
        for entry in body["workflows"]:
            assert set(entry) >= {"name", "description", "steps", "inputs"}
            assert isinstance(entry["inputs"], list)
        smoke = next(entry for entry in body["workflows"] if entry["name"] == "smoke-echo")
        assert "message" in smoke["inputs"]

    def test_execution_graph_shape(self, client):
        body = client.get("/api/execution-graph").json()
        assert set(body) >= {"nodes", "links", "runs", "counts"}
        assert set(body["counts"]) == {"tasks", "delegations", "dependencies", "handoffs", "runs"}

    def test_zulip_shape(self, client):
        """GET /api/zulip exposes the same coordination payload /api/overview
        carries, as a standalone route for the Wayfinder Zulip pane (#918)."""
        body = client.get("/api/zulip").json()
        assert set(body) >= {"site", "worker", "recent_inbound", "stats"}
        assert isinstance(body["recent_inbound"], list)
        overview_zc = client.get("/api/overview").json()["zulip_coordination"]
        assert set(body) == set(overview_zc)

    def test_review_approval_shape(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kw: {
                "tasks": [
                    {
                        "id": 10,
                        "title": "review wait",
                        "status": "doing",
                        "notes": "[waiting-for: review]",
                    }
                ]
            },
        )
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "approval_pending",
            lambda **kw: {"pending": [{"id": 1, "tool": "run_cmd", "status": "pending"}]},
        )
        res = client.get("/api/review-approval")
        assert res.status_code == 200
        body = res.json()
        assert "reviews" in body and "approvals" in body and "counts" in body
        assert body["counts"]["reviews"] == 1
        assert body["counts"]["approvals"] == 1

    def test_review_approval_redacts_payloads(self, client, monkeypatch):
        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", lambda **kw: {"tasks": []})
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "approval_pending",
            lambda **kw: {
                "pending": [
                    {
                        "id": 1,
                        "tool": "run_cmd",
                        "requested_by": "Desktop_Agent1",
                        "status": "pending",
                        "payload": {"command": "secret"},
                    }
                ],
                "approved_unconsumed": [
                    {
                        "id": 2,
                        "tool": "fs_write",
                        "requested_by": "Desktop_Agent1",
                        "status": "approved",
                        "payload": {"path": "/tmp/private"},
                    }
                ],
            },
        )

        review = client.get("/api/review-approval").json()
        approvals = client.get("/api/approvals").json()

        assert "payload" not in review["approvals"][0]
        assert "payload" not in approvals["pending"][0]
        assert "payload" not in approvals["approved_unconsumed"][0]
        assert approvals["pending"][0]["tool"] == "run_cmd"
        assert approvals["approved_unconsumed"][0]["tool"] == "fs_write"

    def test_control_center_rows_keep_platform_object_identity(self, client, monkeypatch):
        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {
                        "id": 41,
                        "title": "object-backed task",
                        "status": "doing",
                        "claimed_by": "Desktop_Agent4",
                    }
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "approval_pending",
            lambda **kwargs: {
                "pending": [
                    {
                        "id": 7,
                        "tool": "run_cmd",
                        "status": "pending",
                        "payload": {"task_id": 41},
                    }
                ]
            },
        )
        monkeypatch.setattr(
            webui_api.registry,
            "list_sessions",
            lambda **kwargs: [
                {
                    "id": "session-41",
                    "agent": "Desktop_Agent4",
                    "status": "running",
                    "task_id": 41,
                    "delegation_id": None,
                    "meta": {},
                }
            ],
        )

        task = client.get("/api/tasks").json()["active"][0]
        approval = client.get("/api/approvals").json()["pending"][0]
        session = client.get("/api/sessions").json()["sessions"][0]

        assert task["platform"] == {
            "kind": "task",
            "id": "41",
            "uri": "unified-agent://tasks/41",
        }
        assert task["links"]["agent"]["id"] == "Desktop_Agent4"
        assert approval["platform"]["uri"] == "unified-agent://approvals/7"
        assert approval["links"]["task"]["id"] == "41"
        assert session["platform"]["uri"] == "unified-agent://sessions/session-41"
        assert session["links"]["task"]["uri"] == "unified-agent://tasks/41"

    def test_approval_detail_shape(self, client, monkeypatch):
        """GET /api/approvals/{id} — task #1263 drawer detail read.

        Uses a real queued row (via check_approval_gate) rather than
        monkeypatching approval_pending: the detail route reads
        tools.approvals.get_approval directly, not the REGISTERED_TOOLS entry.
        """
        from unified_agent import registry
        from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "work_product_release")
        registry.touch_agent("Desktop_Agent1", via="heartbeat_tool", host="desktop-01", force=True)
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate(
                "work_product_release",
                {
                    "task_id": 41,
                    "work_product_id": 9,
                    "title": "Q3 board memo",
                    "content_sha256": "a" * 64,
                    "template_pack": "board-memo-v1",
                    "reviewer_role": "cfo",
                    "previous_version_id": None,
                    "pr_url": "https://github.com/anonb3ll/unified-agent/pull/701",
                },
                requested_by="Desktop_Agent1",
            )
        approval_id = exc_info.value.approval_id

        res = client.get(f"/api/approvals/{approval_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == approval_id
        assert body["tool"] == "work_product_release"
        assert body["status"] == "pending"
        assert body["outcome"] == "requested"
        assert body["requested_by"] == "Desktop_Agent1"
        assert body["device"] == "desktop-01"
        # #1217 honesty rule applied to devices: a heartbeat_tool-reported host
        # is a self-reported claim, not a confirmed one — must render unverified.
        assert body["device_verified"] is False
        assert body["gate"] == {
            "kind": "global_allowlist",
            "label": "Global allowlist (UA_APPROVAL_TOOLS)",
        }
        assert body["payload"]["work_product_id"] == 9
        assert body["payload"]["title"] == "Q3 board memo"
        assert body["payload"]["content_sha256"] == "a" * 64
        assert body["payload"]["template_pack"] == "board-memo-v1"
        assert body["payload"]["reviewer_role"] == "cfo"
        assert body["pr_url"] == "https://github.com/anonb3ll/unified-agent/pull/701"
        assert body["links"]["task"]["id"] == "41"
        assert body["platform"]["uri"] == f"unified-agent://approvals/{approval_id}"

    def test_approval_detail_device_verified_only_for_hub_request(self, client, monkeypatch):
        """#1217 honesty rule: only a hub-observed request counts as verified."""
        from unified_agent import registry
        from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "work_product_release")
        registry.touch_agent("Desktop_Agent1", via="hub_request", host="desktop-02", force=True)
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate(
                "work_product_release",
                {"task_id": 41},
                requested_by="Desktop_Agent1",
            )
        approval_id = exc_info.value.approval_id

        body = client.get(f"/api/approvals/{approval_id}").json()
        assert body["device"] == "desktop-02"
        assert body["device_verified"] is True

    def test_approval_detail_redacts_secret_shaped_payload_values(self, client, monkeypatch):
        from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate(
                "run_cmd",
                {"command": "echo hi", "api_key": "sk-abcdefghijklmnopqrstuvwx"},
                requested_by="Desktop_Agent1",
            )
        approval_id = exc_info.value.approval_id

        body = client.get(f"/api/approvals/{approval_id}").json()
        assert body["payload"]["command"] == "echo hi"
        assert body["payload"]["api_key"] == "[REDACTED_SECRET]"

    def test_approval_detail_unknown_id_is_404(self, client):
        res = client.get("/api/approvals/999999")
        assert res.status_code == 404
        assert "error" in res.json()

    def test_approval_detail_invalid_id_is_400(self, client):
        res = client.get("/api/approvals/not-a-number")
        assert res.status_code == 400
        assert "error" in res.json()

    def test_memory_browse_and_search_shape(self, client, monkeypatch):
        def fake_list(**kwargs):
            assert kwargs.get("tag") == "project:agent"
            assert kwargs.get("limit") == 50
            return {
                "items": [
                    {
                        "id": "note1",
                        "title": "Browse hit",
                        "tags": ["project:agent"],
                        "author": "Desktop_Agent4",
                        "updated_at": "2026-09-06T00:00:00+00:00",
                    }
                ]
            }

        def fake_search(**kwargs):
            assert kwargs.get("query") == "hub"
            assert kwargs.get("tags") == ["project:agent"]
            assert kwargs.get("limit") == 25
            return {
                "results": [
                    {
                        "id": "note2",
                        "title": "Search hit",
                        "tags": ["project:agent", "fact"],
                        "author": "Desktop_Agent4",
                        "updated_at": "2026-09-06T01:00:00+00:00",
                        "snippet": "hub memory",
                    }
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "memory_list", fake_list)
        monkeypatch.setitem(server.REGISTERED_TOOLS, "memory_search", fake_search)

        browse = client.get("/api/memory", params={"tag": "project:agent"}).json()
        assert browse["mode"] == "browse"
        assert browse["tag"] == "project:agent"
        assert browse["total"] == 1
        assert browse["results"][0]["id"] == "note1"
        assert browse["items"][0]["id"] == "note1"

        search = client.get(
            "/api/memory",
            params={"q": "hub", "tag": "project:agent", "limit": 25},
        ).json()
        assert search["mode"] == "search"
        assert search["query"] == "hub"
        assert search["results"][0]["id"] == "note2"
        assert search["total"] == 1

    def test_memory_surfaces_tool_failure(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "memory_list",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("memory list error")),
        )
        res = client.get("/api/memory")
        assert res.status_code == 200
        body = res.json()
        assert "error" in body
        assert "memory_list failed" in body["error"]
        assert body["results"] == []
        assert body["items"] == []

    def test_executor_delegation_shape(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "delegate_list",
            lambda **kw: {
                "delegations": [
                    {
                        "id": 100,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "pending",
                    }
                ]
            },
        )

        monkeypatch.setattr(
            webui_api,
            "survey_worktrees",
            lambda: [
                {
                    "path": ".worktrees/task-100",
                    "root": ".",
                    "in_workspace": True,
                    "disposable": True,
                    "reason": "idle",
                    "task_id": 100,
                    "stale": True,
                    "stale_reason": "idle_timeout",
                }
            ],
        )
        res = client.get("/api/executor-delegation")
        assert res.status_code == 200
        body = res.json()
        assert "delegations" in body and "stale_worktrees" in body and "counts" in body
        assert body["total_delegations"] == 1

    def test_executor_delegation_reports_health_and_failures(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "delegate_list",
            lambda **kw: {
                "delegations": [
                    {
                        "id": 1,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "pending",
                    },
                    {
                        "id": 2,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "in_flight",
                    },
                    {
                        "id": 3,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "completed",
                    },
                    {
                        "id": 4,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "cancelled",
                    },
                    {
                        "id": 5,
                        "from_agent": "Desktop_Agent1",
                        "to_agent": "Desktop_agent3",
                        "status": "failed",
                        "callback_result": "Execution failed (error): boom",
                    },
                ]
            },
        )

        monkeypatch.setattr(
            webui_api,
            "survey_worktrees",
            lambda: [
                {
                    "path": ".worktrees/task-100",
                    "root": ".",
                    "in_workspace": True,
                    "disposable": True,
                    "reason": "idle",
                    "task_id": 100,
                    "stale": True,
                    "stale_reason": "idle_timeout",
                },
                {
                    "path": ".worktrees/task-200",
                    "root": ".",
                    "in_workspace": True,
                    "disposable": True,
                    "reason": "unmerged",
                    "task_id": 200,
                    "stale": True,
                    "stale_reason": "idle_timeout",
                },
            ],
        )
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "system_doctor",
            lambda **kw: {
                "summary": {"live": 1, "dormant": 0, "misconfigured": 0, "unknown": 0},
                "checks": [
                    {
                        "capability": "active_executor_service",
                        "status": "LIVE",
                        "detail": "unified-agent-executor.service: active",
                    }
                ],
            },
        )

        webui_api._doctor_cache = None

        body = client.get("/api/executor-delegation").json()

        assert body["counts"] == {
            "pending": 1,
            "running": 1,
            "completed": 1,
            "cancelled": 1,
            "error": 1,
        }
        assert body["health"]["summary"]["live"] == 1
        assert body["recent_failures"][0]["id"] == 5
        assert body["recent_failures"][0]["status"] == "error"
        assert body["recent_failures"][0]["detail"] == "Execution failed (error): boom"
        assert len(body["stale_worktrees"]) == 2

    def test_review_approval_surfaces_tool_failure(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("task list error")),
        )
        res = client.get("/api/review-approval")
        assert res.status_code == 200
        body = res.json()
        assert "error" in body
        assert "errors" in body
        assert "task_list failed" in body["error"]
        assert body["reviews"] == []

    def test_executor_delegation_surfaces_tool_failure(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "delegate_list",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("delegate list error")),
        )
        res = client.get("/api/executor-delegation")
        assert res.status_code == 200
        body = res.json()
        assert "error" in body
        assert "errors" in body
        assert "delegate_list failed" in body["error"]
        assert body["delegations"] == []

    def test_executor_delegation_keeps_rows_when_worktree_survey_is_denied(
        self, client, monkeypatch
    ):
        """A Desktop_Person DENY must not blank delegations that already loaded."""
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "delegate_list",
            lambda **kw: {
                "delegations": [
                    {
                        "id": 8,
                        "from_agent": "Desktop_Person",
                        "to_agent": "Desktop_agent3",
                        "status": "completed",
                    }
                ]
            },
        )

        def boom():
            raise ToolError(
                "Hub-mediated filesystem access is denied for agent 'Desktop_Person' "
                "(explicit UA_AGENT_WORKSPACES DENY entry); use this identity's "
                "own native file tools instead."
            )

        monkeypatch.setattr(webui_api, "survey_worktrees", boom)
        body = client.get("/api/executor-delegation").json()
        assert body["delegations"][0]["id"] == 8
        assert body["counts"]["completed"] == 1
        assert "error" not in body
        assert "denied" in body["worktree_error"]
        assert body["stale_worktrees"] == []

    def test_queue_health_categorization(self, client, monkeypatch):
        def fake_task_list(**kwargs):
            assert kwargs["fields"] == "compact"
            assert kwargs["status"] in {"open", "doing", "waiting_review", "blocked"}
            assert kwargs["limit"] == 500
            return {
                "tasks": [
                    {
                        "id": 1,
                        "title": "desktop open task",
                        "status": "open",
                    },
                    {
                        "id": 2,
                        "title": "review task",
                        "status": "open",
                        "waiting_for_review": True,
                    },
                    {
                        "id": 3,
                        "title": "doing active task",
                        "status": "doing",
                        "claimed_by": "Desktop_Agent1",
                        "lock_expires_at": "2099-01-01T00:00:00+00:00",
                    },
                    {
                        "id": 4,
                        "title": "stale doing task",
                        "status": "doing",
                        "claimed_by": "Desktop_agent3",
                        "lock_expires_at": "2020-01-01T00:00:00+00:00",
                    },
                    {
                        "id": 5,
                        "title": "laptop task",
                        "status": "open",
                        "laptop_lane": True,
                    },
                    {
                        "id": 6,
                        "title": "blocked task",
                        "status": "blocked",
                    },
                    {
                        "id": 7,
                        "title": "archived task",
                        "status": "open",
                        "notes_archived": True,
                    },
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()

        assert body["counts"] == {
            "desktop_open": 1,
            "dispatch_ready": 0,
            "interactive_only": 0,
            "operator_only": 0,
            "legacy_operator_only": 0,
            "doing": 2,
            "waiting_review": 1,
            "stale_claims": 1,
            "laptop_lane": 1,
            "blocked_archived": 2,
            "inconsistent": 0,
            "other": 0,
            "total_active": 4,
        }
        assert [t["id"] for t in body["desktop_open"]] == [1]
        assert [t["id"] for t in body["waiting_review"]] == [2]
        assert [t["id"] for t in body["doing"]] == [3, 4]
        assert [t["id"] for t in body["stale_claims"]] == [4]
        assert [t["id"] for t in body["laptop_lane"]] == [5]
        assert [t["id"] for t in body["blocked_archived"]] == [6, 7]

    def test_queue_health_real_waiting_review_and_cancelled_and_unknown_status(
        self, client, monkeypatch
    ):
        """task-lifecycle-contract.md §6 Q5/Q6 and §1.4 step 3.

        A real 'waiting_review' status (not just the notes marker) must land
        in the same bucket as the marker form. 'done' and 'cancelled' are both
        terminal and intentionally excluded from this active-queue view. A
        genuinely unrecognized future status must degrade to 'other', not
        vanish silently.
        """

        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {"id": 1, "title": "real waiting_review", "status": "waiting_review"},
                    {"id": 2, "title": "done", "status": "done", "completed_at": "x"},
                    {"id": 3, "title": "cancelled", "status": "cancelled"},
                    {"id": 4, "title": "from the future", "status": "some_new_status"},
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()

        assert [t["id"] for t in body["waiting_review"]] == [1]
        assert body["other"][0]["id"] == 4
        assert body["other"][0]["title"] == "from the future"
        assert body["other"][0]["status"] == "some_new_status"
        assert body["counts"]["other"] == 1
        assert body["counts"]["inconsistent"] == 0
        all_bucketed_ids = {
            t["id"]
            for bucket in (
                "desktop_open",
                "doing",
                "waiting_review",
                "laptop_lane",
                "blocked_archived",
                "inconsistent",
                "other",
            )
            for t in body[bucket]
        }
        assert 2 not in all_bucketed_ids  # done: intentionally unbucketed
        assert 3 not in all_bucketed_ids  # cancelled: same as done

    def test_queue_health_coerces_sqlite_int_flags_to_bool(self, client, monkeypatch):
        """Task #958: SQLite 0/1 integer flags must be coerced to booleans in queue_health."""

        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {
                        "id": 10,
                        "title": "task with int flags",
                        "status": "open",
                        "waiting_for_review": 1,
                        "laptop_lane": 0,
                        "notes_archived": 0,
                    }
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()
        assert len(body["waiting_review"]) == 1
        task = body["waiting_review"][0]
        assert task["waiting_for_review"] is True
        assert task["laptop_lane"] is False
        assert task["notes_archived"] is False

    def test_queue_health_categorization_from_notes(self, client, monkeypatch):
        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {
                        "id": 1,
                        "title": "desktop open task",
                        "status": "open",
                    },
                    {
                        "id": 2,
                        "title": "review task",
                        "status": "open",
                        "notes": "Task ready [waiting-for: review]",
                    },
                    {
                        "id": 5,
                        "title": "laptop task",
                        "status": "open",
                        "lane": "laptop",
                    },
                    {
                        "id": 7,
                        "title": "archived task",
                        "status": "open",
                        "notes": "Old task [archived]",
                    },
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()

        assert body["counts"]["desktop_open"] == 1
        assert body["counts"]["waiting_review"] == 1
        assert body["counts"]["laptop_lane"] == 1
        assert body["counts"]["blocked_archived"] == 1

    def test_queue_health_separates_ready_and_interactive_and_sorts_urgency(
        self, client, monkeypatch
    ):
        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {
                        "id": 1,
                        "title": "later",
                        "status": "open",
                        "lane": "dispatch-ready",
                        "priority": "high",
                        "urgency": "later",
                        "created_at": "2026-09-01T00:00:00+00:00",
                    },
                    {
                        "id": 2,
                        "title": "next",
                        "status": "open",
                        "lane": "dispatch-ready",
                        "priority": "med",
                        "urgency": "next",
                        "created_at": "2026-09-01T00:00:00+00:00",
                    },
                    {
                        "id": 3,
                        "title": "now younger",
                        "status": "open",
                        "lane": "dispatch-ready",
                        "priority": "high",
                        "urgency": "now",
                        "created_at": "2026-09-02T00:00:00+00:00",
                    },
                    {
                        "id": 4,
                        "title": "now older",
                        "status": "open",
                        "lane": "dispatch-ready",
                        "priority": "high",
                        "urgency": "now",
                        "created_at": "2026-09-01T00:00:00+00:00",
                    },
                    {"id": 5, "title": "operator", "status": "open", "lane": "interactive-only"},
                    {
                        "id": 6,
                        "title": "Operator",
                        "status": "open",
                        "lane": "legacy-operator-only",
                    },
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()
        assert [task["id"] for task in body["dispatch_ready"]] == [4, 3, 2, 1]
        assert [task["id"] for task in body["interactive_only"]] == [5]
        assert [task["id"] for task in body["operator_only"]] == [6]
        assert body["legacy_operator_only"] == body["operator_only"]
        assert body["counts"]["legacy_operator_only"] == body["counts"]["operator_only"] == 1
        assert body["counts"]["total_active"] == 6
        assert body["dispatch_wip"]["cap"] >= 1
        assert body["dispatch_wip"]["in_flight"] >= 0

    def test_queue_health_and_tasks_see_live_work_when_done_history_fills_default_window(
        self, client, monkeypatch
    ):
        """task #685: status=all + default limit 100 is filled by done rows.

        The Action Deck picker reads queue_health.desktop_open + tasks.active.
        A live open (and doing) task must still appear when ≥100 done tasks
        would otherwise consume the default window.
        """
        live_open = {
            "id": 999,
            "title": "live desktop open task",
            "status": "open",
            "project": "unified-agent",
        }
        live_doing = {
            "id": 998,
            "title": "live doing task",
            "status": "doing",
            "claimed_by": "Desktop_Agent4",
            "lock_expires_at": "2099-01-01T00:00:00+00:00",
            "project": "unified-agent",
        }
        done_window = [
            {
                "id": i,
                "title": f"done {i}",
                "status": "done",
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
            for i in range(1, 101)
        ]

        def fake_task_list(**kwargs):
            status = kwargs.get("status", "open")
            limit = kwargs.get("limit", 100)
            if status == "all":
                return {"tasks": done_window[:limit]}
            if status == "open":
                return {"tasks": [live_open]}
            if status == "doing":
                return {"tasks": [live_doing]}
            return {"tasks": []}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        health = client.get("/api/queue-health").json()
        tasks = client.get("/api/tasks").json()

        assert [t["id"] for t in health["desktop_open"]] == [999]
        assert [t["id"] for t in health["doing"]] == [998]
        assert health["counts"]["desktop_open"] == 1
        assert health["counts"]["doing"] == 1
        assert health["counts"]["total_active"] == 2
        assert [t["id"] for t in tasks["active"]] == [998]
        assert tasks["buckets"].get("open") == 1
        assert tasks["buckets"].get("doing") == 1

    def test_attention_surfaces_every_waiting_kind(self, client, monkeypatch):
        def fake_task_list(**kwargs):
            if kwargs.get("notes_contain") == "[waiting-for: review]":
                return {
                    "tasks": [
                        {
                            "id": 7,
                            "title": "review me",
                            "updated_at": "2026-07-28T09:00:00+00:00",
                        }
                    ]
                }
            if kwargs.get("status") == "doing":
                return {
                    "tasks": [
                        {
                            "id": 8,
                            "title": "stale claim",
                            "claimed_by": "Desktop_agent3",
                            "lock_expires_at": "2026-07-28T10:00:00+00:00",
                        }
                    ]
                }
            return {"tasks": []}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "approval_pending",
            lambda **kw: {
                "pending": [
                    {
                        "id": 9,
                        "tool": "run_cmd",
                        "created_at": "2026-07-28T08:00:00+00:00",
                    }
                ]
            },
        )
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "check_anomalies",
            lambda **kw: {
                "anomalies": [
                    {
                        "rule": "service_down",
                        "subject": "service:unified-agent",
                        "detail": "unit is down",
                    }
                ]
            },
        )

        monkeypatch.setattr(
            webui_api,
            "survey_worktrees",
            lambda: [
                {
                    "path": ".worktrees/task-42",
                    "root": ".",
                    "in_workspace": True,
                    "disposable": True,
                    "reason": "idle_timeout",
                    "task_id": 42,
                    "stale": True,
                    "stale_reason": "idle_timeout",
                }
            ],
        )

        body = client.get("/api/attention").json()

        assert body["counts"] == {
            "review_wait": 1,
            "approval": 1,
            "anomaly": 1,
            "stale_worktree": 1,
            "stale_claim": 1,
        }
        assert [item["kind"] for item in body["items"]] == [
            "anomaly",
            "approval",
            "review_wait",
            "stale_claim",
            "stale_worktree",
        ]
        assert body["items"][0]["source"] == {"anomaly": "service_down:service:unified-agent"}
        assert "source_host" in body["items"][0]
        assert body["items"][0]["hub_origin"] == "local"
        assert body["items"][1]["source"] == {"approval_id": 9}
        assert body["items"][2]["source"] == {"task_id": 7}
        assert body["items"][3]["source"] == {"task_id": 8}
        assert body["items"][4]["source"] == {"worktree": ".worktrees/task-42"}
        assert body["items"][4]["reason"] == "worktree idle_timeout"

    def test_attention_caps_stale_worktrees(self, client, monkeypatch):
        """P2 noise: dozens of idle worktrees must not bury needs-Operator items."""

        many = [
            {
                "path": f".worktrees/task-{i}",
                "root": ".",
                "in_workspace": True,
                "disposable": True,
                "reason": "idle_timeout",
                "task_id": i,
                "stale": True,
                "stale_reason": "idle_timeout",
            }
            for i in range(12)
        ]
        monkeypatch.setattr(webui_api, "survey_worktrees", lambda: many)

        body = client.get("/api/attention").json()
        stale_items = [i for i in body["items"] if i["kind"] == "stale_worktree"]
        assert len(stale_items) == webui_api._STALE_WORKTREE_ATTENTION_CAP
        assert body["counts"]["stale_worktree"] == webui_api._STALE_WORKTREE_ATTENTION_CAP
        overflow = [
            i for i in body.get("informational", []) if i.get("kind") == "stale_worktree_overflow"
        ]
        assert len(overflow) == 1
        assert "cleanup_stale_worktrees" in overflow[0]["reason"]
        assert "not auto-deleted" in overflow[0]["reason"]

    def test_attention_filters_cleared_anomalies(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "check_anomalies",
            lambda **kw: {
                "anomalies": [
                    {
                        "rule": "thrash",
                        "subject": "task:149",
                        "detail": "thrash cleared",
                        "status": "cleared",
                    },
                    {
                        "rule": "disk_space",
                        "subject": "host:desktop",
                        "detail": "low disk",
                        "status": "active",
                    },
                ]
            },
        )
        body = client.get("/api/attention").json()
        anomaly_items = [i for i in body.get("items", []) if i["kind"] == "anomaly"]
        assert len(anomaly_items) == 1
        assert anomaly_items[0]["reason"] == "low disk"

    def test_attention_ignores_done_review_markers(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 27,
                        "title": "stale completed review",
                        "status": "done",
                        "updated_at": "2026-07-28T09:00:00+00:00",
                    }
                ]
                if kwargs.get("notes_contain") == "[waiting-for: review]"
                else [],
            },
        )
        body = client.get("/api/attention").json()

        assert body["counts"] == {}
        assert body["items"] == []

    def test_attention_ignores_terminal_review_evidence_with_legacy_marker(
        self, client, monkeypatch
    ):
        terminal_records = (
            (439, "[review: merged-upstream]"),
            (440, "[review: no-merge]"),
            (453, "[review: merged-upstream]"),
            (454, "[review: merged-upstream]"),
            (484, "[resolved-evidence: 2026-08-19]"),
        )

        def fake_task_list(**kwargs):
            if kwargs.get("notes_contain") != "[waiting-for: review]":
                return []
            return {
                "tasks": [
                    {
                        "id": task_id,
                        "title": f"terminal task {task_id}",
                        "status": "done",
                        "notes": f"[waiting-for: review]\n{marker}",
                        "updated_at": "2026-08-19T09:00:00+00:00",
                    }
                    for task_id, marker in terminal_records
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/attention").json()

        assert body["counts"] == {}
        assert body["items"] == []

    def test_attention_labels_waiting_review_as_awaiting_operator(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 473,
                        "title": "Telemetry evidence cycle",
                        "status": "waiting_review",
                        "notes": "[waiting-for: review]",
                        "updated_at": "2026-08-19T09:00:00+00:00",
                    }
                ]
                if kwargs.get("notes_contain") == "[waiting-for: review]"
                else [],
            },
        )
        body = client.get("/api/attention").json()

        item = next(i for i in body["items"] if i["source"] == {"task_id": 473})
        assert item["kind"] == "awaiting_operator"
        assert item["reason"] == "awaiting the operator's review decision"

    def test_attention_surfaces_unregistered_agent_labels(self, client, monkeypatch):
        """Command Center attention view (Codex, 2026-08-09 approved slice): identity drift."""
        from unified_agent import registry

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.touch_agent("rogue-cron", via="test", force=True)

        body = client.get("/api/attention").json()
        drift_items = [i for i in body["items"] if i["kind"] == "identity_drift"]
        assert len(drift_items) == 1
        assert drift_items[0]["source"] == {"agent_label": "rogue-cron"}
        assert drift_items[0]["severity"] == "low"
        assert "not in UA_AGENT_LABELS" in drift_items[0]["reason"]
        assert body["counts"]["identity_drift"] == 1

    def test_attention_treats_supported_human_identity_as_expected(self, client, monkeypatch):
        from unified_agent import registry

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.touch_agent("Desktop_Person", via="test", force=True)

        body = client.get("/api/attention").json()

        assert not any(
            i["source"] == {"agent_label": "Desktop_Person"}
            for i in body["items"]
            if i["kind"] == "identity_drift"
        )

    def test_attention_ignores_configured_labels_never_seen(self, client, monkeypatch):
        """A configured-but-never-launched label is not drift and not staleness."""
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Never_Launched")

        body = client.get("/api/attention").json()
        assert "identity_drift" not in body["counts"]
        assert "stale_agent" not in body["counts"]

    def test_attention_surfaces_unexpected_stale_configured_agents(self, client, monkeypatch):
        """A non-topology agent that goes quiet remains actionable."""
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_Runtime")
        registry.touch_agent("Desktop_Runtime", via="test", force=True)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agents SET last_seen = ? WHERE label = ?",
                ("2020-01-01T00:00:00+00:00", "Desktop_Runtime"),
            )
            conn.commit()
        finally:
            conn.close()

        body = client.get("/api/attention").json()
        stale_items = [i for i in body["items"] if i["kind"] == "stale_agent"]
        assert len(stale_items) == 1
        assert stale_items[0]["source"] == {"agent_label": "Desktop_Runtime"}
        assert stale_items[0]["severity"] == "medium"
        assert stale_items[0]["reason"] == "configured agent has gone offline"

    def _make_stale(self, monkeypatch, label):
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", f"Desktop_Agent1:{label}")
        registry.touch_agent(label, via="test", force=True)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agents SET last_seen = ? WHERE label = ?",
                ("2020-01-01T00:00:00+00:00", label),
            )
            conn.commit()
        finally:
            conn.close()

    def test_attention_pages_on_a_quiet_laptop_agent(self, client, monkeypatch):
        """Single-hub topology (2026-08-20): laptop agents reach the desktop hub
        directly over Tailscale and heartbeat like any other agent, so a quiet
        laptop is a real outage signal. An earlier revision of this feature
        suppressed all four Laptop_* labels by default, which would have hidden
        exactly the signal the consolidation was meant to provide."""
        self._make_stale(monkeypatch, "Laptop_Agent5")

        body = client.get("/api/attention").json()

        item = next(i for i in body["items"] if i["source"] == {"agent_label": "Laptop_Agent5"})
        assert item["severity"] in ("low", "medium")
        assert item["reason"].startswith("configured agent has gone")
        assert not any(
            i["source"] == {"agent_label": "Laptop_Agent5"} for i in body["informational"]
        )

    def test_attention_honors_the_expected_offline_seam(self, client, monkeypatch):
        """The suppression mechanism survives for a tier that genuinely cannot
        report — it is now opt-in per label, never a default."""
        monkeypatch.setenv("UA_EXPECTED_OFFLINE_AGENT_LABELS", "Laptop_Agent5")
        self._make_stale(monkeypatch, "Laptop_Agent5")

        body = client.get("/api/attention").json()

        assert not any(i["source"] == {"agent_label": "Laptop_Agent5"} for i in body["items"])
        info = next(
            i for i in body["informational"] if i["source"] == {"agent_label": "Laptop_Agent5"}
        )
        assert info["reason"] == "offline state expected by configured topology"

    def test_attention_surfaces_a_killed_autonomous_cycle(self, client):
        """Task #373 Tier 2, 3.5: autonomous-cycle state was entirely absent
        from this view before — not even listed as a known gap."""
        from unified_agent.tools import cycles as cycles_tool

        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3")["cycle"]["id"]
        cycles_tool.autonomous_cycle_kill(cycle_id, "Desktop_agent3", "smoke test kill")

        body = client.get("/api/attention").json()
        cycle_items = [i for i in body["items"] if i["kind"] == "autonomous_cycle_stopped"]
        assert len(cycle_items) == 1
        assert cycle_items[0]["source"] == {"cycle_id": cycle_id}
        assert cycle_items[0]["severity"] == "medium"
        assert "smoke test kill" in cycle_items[0]["reason"]
        assert body["counts"]["autonomous_cycle_stopped"] == 1

    def test_attention_surfaces_a_blocked_over_budget_cycle_with_task(self, client):
        from unified_agent.tools import cycles as cycles_tool
        from unified_agent.tools import tasks as tasks_tool

        task_id = tasks_tool.task_add("cycle attention test task")["id"]
        cycle_id = cycles_tool.autonomous_cycle_start(
            "Desktop_agent3", task_id=task_id, budgets={"time_s": 10}
        )["cycle"]["id"]
        cycles_tool.autonomous_cycle_record_usage(cycle_id, "Desktop_agent3", elapsed_s=20)

        body = client.get("/api/attention").json()
        cycle_items = [i for i in body["items"] if i["kind"] == "autonomous_cycle_stopped"]
        assert len(cycle_items) == 1
        assert cycle_items[0]["source"] == {"cycle_id": cycle_id, "task_id": task_id}
        assert cycle_items[0]["severity"] == "medium"
        assert "budget exhausted" in cycle_items[0]["reason"]
        assert f"task #{task_id}" in cycle_items[0]["title"]

    def test_attention_ignores_healthy_cycle_states(self, client):
        """plan/delegated/executing/.../completed are not attention-worthy —
        only failed/blocked/killed are "stopped, not by success"."""
        from unified_agent.tools import cycles as cycles_tool

        cycles_tool.autonomous_cycle_start("Desktop_agent3")  # stays in 'plan'

        body = client.get("/api/attention").json()
        assert "autonomous_cycle_stopped" not in body["counts"]

    def test_attention_surfaces_human_zulip_request(self, client, monkeypatch):
        monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")
        from unified_agent import events

        events.record_event(
            "zulip.inbound",
            payload={
                "source": "zulip",
                "message_id": 9001,
                "channel": "requests",
                "topic": "adhoc-request",
                "sender_email": "operator@zulip.example.test",
                "sender_full_name": "Example Operator",
                "content": "please handle this",
                "actionable_for": [],
                "target_reason": "missing-target",
            },
            dedupe_key="zulip:9001",
            notify=False,
        )

        body = client.get("/api/attention").json()
        assert any(item["kind"] == "zulip_request" for item in body["items"])

    def test_attention_surfaces_zulip_actionable_inbound(self, client, monkeypatch):
        monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")
        from unified_agent import events

        events.record_event(
            "zulip.inbound",
            payload={
                "source": "zulip",
                "message_id": 9002,
                "channel": "coord",
                "topic": "task-519",
                "sender_email": "operator@zulip.example.test",
                "sender_full_name": "Example Operator",
                "content": "[target: Desktop_Agent4] check the worker",
                "actionable_for": ["Desktop_Agent4"],
                "target_reason": "explicit-target",
            },
            dedupe_key="zulip:9002",
            notify=False,
        )

        body = client.get("/api/attention").json()
        actionable = [item for item in body["items"] if item["kind"] == "zulip_actionable"]
        assert len(actionable) == 1
        assert actionable[0]["source"]["task_id"] == 519
        assert actionable[0]["source"]["zulip_message_id"] == 9002

    def test_attention_surfaces_zulip_worker_error(self, client, monkeypatch):
        monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")

        async def fake_health():
            return {
                "status": "error",
                "last_error": "poll failed",
                "last_successful_poll": None,
                "channels": [],
            }

        monkeypatch.setattr(webui_api, "zulip_worker_health_payload", fake_health)
        body = client.get("/api/attention").json()
        assert any(item["kind"] == "zulip_worker_degraded" for item in body["items"])

    def test_attention_keeps_intentional_acceptance_smoke_stop_in_history(
        self, client, monkeypatch
    ):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "autonomous_cycle_list",
            lambda **kwargs: {
                "cycles": [
                    {
                        "id": "2e3ab0635aa9",
                        "state": "killed",
                        "stop_reason": "killed by Desktop_Agent2: acceptance smoke cleanup",
                        "updated_at": "2026-08-19T22:27:42+00:00",
                    }
                ]
            },
        )
        body = client.get("/api/attention").json()

        assert "autonomous_cycle_stopped" not in body["counts"]
        info = next(i for i in body["informational"] if i["source"] == {"cycle_id": "2e3ab0635aa9"})
        assert info["severity"] == "info"
        assert "history only" in info["reason"]

    def test_attention_lists_omitted_kinds_with_no_data_source_yet(self, client):
        body = client.get("/api/attention").json()
        omitted_kinds = {o["kind"] for o in body["omitted"]}
        assert omitted_kinds == {"roadmap_conflict", "migration_gate"}

    def test_digest_groups_attention_items_by_kind(self, client, monkeypatch):
        from unified_agent import registry

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        registry.touch_agent("rogue-cron", via="test", force=True)

        body = client.get("/api/digest").json()
        assert body["total"] == 1
        section = next(s for s in body["sections"] if s["kind"] == "identity_drift")
        assert section["count"] == 1
        assert section["items"][0]["source"] == {"agent_label": "rogue-cron"}
        assert "generated_at" in body
        assert body["waiting_on_operator"] == body["waiting_on_legacy_operator"]

    def test_digest_is_empty_on_a_clean_system(self, client):
        body = client.get("/api/digest").json()
        assert body["sections"] == []
        assert body["total"] == 0

    def test_digest_reports_degraded_doctor_health(self, client, monkeypatch):
        async def clean_attention():
            return {"items": [], "counts": {}, "total": 0, "errors": [], "omitted": []}

        async def empty_agents():
            return {"agents": []}

        async def empty_sessions(limit=100):
            return {"sessions": []}

        async def empty_cycles(limit=50):
            return {"cycles": []}

        async def degraded_health(force=False):
            return {"summary": {"misconfigured": 0, "degraded": 1}, "checks": []}

        monkeypatch.setattr(webui_api, "attention_payload", clean_attention)
        monkeypatch.setattr(webui_api, "agents_payload", empty_agents)
        monkeypatch.setattr(webui_api, "sessions_payload", empty_sessions)
        monkeypatch.setattr(webui_api, "cycles_payload", empty_cycles)
        monkeypatch.setattr(webui_api, "health_payload", degraded_health)
        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", lambda **_kwargs: {"tasks": []})

        body = client.get("/api/digest").json()
        assert body["status"] == "degraded"
        assert "system health is degraded" in body["evidence"]

    def test_queue_health_flags_completion_timestamp_contradictions(self, client, monkeypatch):
        """status and completed_at must agree, independent of the notes markers."""

        def fake_task_list(**kwargs):
            return {
                "tasks": [
                    {"id": 1, "title": "done, never stamped", "status": "done"},
                    {
                        "id": 2,
                        "title": "stamped but still open",
                        "status": "open",
                        "completed_at": "2026-08-01T00:00:00+00:00",
                    },
                    {
                        "id": 3,
                        "title": "properly done",
                        "status": "done",
                        "completed_at": "2026-08-01T00:00:00+00:00",
                    },
                ]
            }

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_list", fake_task_list)
        body = client.get("/api/queue-health").json()

        assert [t["id"] for t in body["inconsistent"]] == [1, 2]
        assert body["counts"]["inconsistent"] == 2

    def test_whoami_reports_control_enabled(self, client):
        body = client.get("/api/whoami").json()
        assert body["control_enabled"] is True

    def test_unknown_agent_is_404(self, client):
        assert client.get("/api/agents/nobody").status_code == 404

    def test_unknown_session_is_404(self, client):
        assert client.get("/api/sessions/nosuchid").status_code == 404

    def test_agent_detail_includes_sessions_and_activity(self, client):
        from unified_agent import registry

        registry.touch_agent("Desktop_Agent1", via="test", force=True)
        registry.start_session("Desktop_Agent1", "pi_run")
        body = client.get("/api/agents/Desktop_Agent1").json()
        assert body["agent"]["label"] == "Desktop_Agent1"
        assert len(body["sessions"]) == 1
        assert "activity" in body

    def test_a_failing_tool_degrades_that_pane_not_the_page(self, client, monkeypatch):
        """A dashboard is least useful if one bad pane 500s the whole thing."""
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "check_anomalies",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        res = client.get("/api/overview")
        assert res.status_code == 200
        assert "error" in res.json()["anomalies"]


class TestOrigin:
    def test_hostile_origin_rejected(self, client):
        res = client.get("/api/overview", headers={"Origin": "http://evil.example"})
        assert res.status_code == 403

    def test_own_origin_allowed(self, client):
        res = client.get("/api/overview", headers={"Origin": "http://127.0.0.1:8765"})
        assert res.status_code == 200

    def test_absent_origin_allowed(self, client):
        """curl and same-origin navigations send none; MCP makes the same choice."""
        assert client.get("/api/overview").status_code == 200

    def test_login_route_also_checks_origin(self, client):
        res = client.post(
            "/api/session", json={"token": TOKEN}, headers={"Origin": "http://evil.example"}
        )
        assert res.status_code == 403


class TestActionErrorsReachTheOperator:
    """Every tool in this repo raises `ToolError` for an operator mistake, and
    `ToolError` derives straight from `Exception` — so the `except (KeyError, ValueError)`
    these three routes shipped with caught none of them. An unknown workflow, a bad task
    status or a stale approval id all returned a bare 500 with no body, which the UI
    could only render as "something went wrong". The other four action routes already
    had the bounded fallback; these now match."""

    def test_unknown_workflow_is_a_400_naming_the_alternatives(self, client):
        res = client.post(
            "/api/action/workflow",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"name": "definitely-not-a-workflow"},
        )
        assert res.status_code == 400
        assert "no workflow definition" in res.json()["error"]

    def test_unknown_task_is_a_400_with_the_tool_message(self, client):
        res = client.post(
            "/api/action/task",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"id": 987654, "status": "doing"},
        )
        assert res.status_code == 400
        assert res.json()["error"]

    def test_unknown_task_update_field_is_a_400_naming_the_key(self, client):
        """task #1246: /api/action/task whitelisted the request body against
        _TASK_UPDATE_PARAMS and silently dropped everything else. A caller that
        sent {"id": ..., "notes_append": "..."} — the natural misremembering
        of the real `append_note` parameter — got a 200, the full task, and a
        bumped updated_at while the note was discarded. Unknown fields now
        fail closed, matching the extra="forbid" MCP path."""
        from unified_agent.tools import tasks as tasks_tool

        task_id = tasks_tool.task_add("unknown-field probe")["id"]

        res = client.post(
            "/api/action/task",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"id": task_id, "notes_append": "[grooming] must not vanish"},
        )

        assert res.status_code == 400
        assert "notes_append" in res.json()["error"]

        # The rejected call must not have partially applied.
        conn = storage.connect()
        try:
            row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            conn.close()
        assert not row["notes"]

    def test_digest_escalation_reuses_task_update_attribution(self, client, monkeypatch):
        calls = []

        async def update_task(**kwargs):
            calls.append(kwargs)
            return {"id": kwargs["task_id"], "status": "open"}

        monkeypatch.setattr(webui_api, "update_task", update_task)
        res = client.post(
            "/api/action/task",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"id": 42, "append_note": "[exception-escalated: evt-1] failure: provider down"},
        )

        assert res.status_code == 200
        assert calls == [
            {
                "task_id": 42,
                "append_note": "[exception-escalated: evt-1] failure: provider down",
                "actor": "Desktop_Agent1",
            }
        ]

    def test_digest_escalation_rejects_non_string_note(self, client):
        res = client.post(
            "/api/action/task",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"id": 42, "append_note": {"message": "bad"}},
        )
        assert res.status_code == 400
        assert "append_note" in res.json()["error"]

    def test_unknown_approval_is_a_400_with_the_tool_message(self, client, monkeypatch):
        human_token = f"human-{secrets.token_urlsafe(8)}"
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{human_token}")
        res = client.post(
            "/api/action/approval",
            headers={"Authorization": f"Bearer {human_token}"},
            json={"id": 987654, "decision": "approve"},
        )
        assert res.status_code == 400
        assert res.json()["error"]

    def test_verified_human_can_resolve_approval_when_agent_access_is_disabled(
        self, client, monkeypatch
    ):
        from unified_agent.tools.approvals import (
            ApprovalPendingError,
            approval_pending,
            check_approval_gate,
        )

        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:operator-tok")
        monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo recovery"})
        approval_id = approval_pending()["pending"][0]["id"]

        res = client.post(
            "/api/action/approval",
            headers={"Authorization": "Bearer operator-tok"},
            json={"id": approval_id, "decision": "approve"},
        )

        assert res.status_code == 200
        assert res.json()["status"] == "approved"

    def test_action_cycle_lifecycle_and_kill(self, client):
        # 1. Start cycle
        start_res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"operation": "start", "budgets": {"tokens": 50000, "time_s": 300}},
        )
        assert start_res.status_code == 201
        data = start_res.json()
        assert "cycle" in data
        cycle_id = data["cycle"]["id"]
        assert data["cycle"]["state"] == "plan"

        # 2. Transition cycle
        trans_res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "operation": "transition",
                "cycle_id": cycle_id,
                "to_state": "delegated",
                "idempotency_key": f"test-key-{cycle_id}-1",
            },
        )
        assert trans_res.status_code == 200
        assert trans_res.json()["cycle"]["state"] == "delegated"

        # 3. Usage recording
        usage_res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "operation": "usage",
                "cycle_id": cycle_id,
                "tokens": 120,
                "elapsed_s": 4.5,
            },
        )
        assert usage_res.status_code == 200
        assert usage_res.json()["cycle"]["used_tokens"] == 120

        # 4. Kill cycle
        kill_res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "operation": "kill",
                "cycle_id": cycle_id,
                "reason": "operator abort",
            },
        )
        assert kill_res.status_code == 200
        assert kill_res.json()["cycle"]["state"] == "killed"
        assert "killed by" in kill_res.json()["cycle"]["stop_reason"]

    def test_action_cycle_invalid_payloads(self, client):
        # Invalid operation
        res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"operation": "unknown_op"},
        )
        assert res.status_code == 400
        assert "operation must be start, transition, usage, or kill" in res.json()["error"]

        # Missing reason on kill
        res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"operation": "kill", "cycle_id": "nonexistent"},
        )
        assert res.status_code == 400
        assert "cycle_id and reason are required" in res.json()["error"]

        # Invalid budgets type
        res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"operation": "start", "budgets": "not-a-dict"},
        )
        assert res.status_code == 400
        assert "budgets must be an object" in res.json()["error"]


class TestDecisionGradeAttentionAndDigest:
    """Decision-grade Command Center attention and exception digest tests (task #439)."""

    def test_attention_surfaces_plan_approval_and_human_merge(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 101,
                        "title": "plan waiting task",
                        "status": "open",
                        "updated_at": "2026-08-19T01:00:00+00:00",
                    }
                ]
                if kwargs.get("notes_contain") == "[waiting-for: plan-approval]"
                else [
                    {
                        "id": 102,
                        "title": "human merge task",
                        "status": "open",
                        "updated_at": "2026-08-19T01:05:00+00:00",
                    }
                ]
                if kwargs.get("notes_contain") == "[waiting-for: human-merge]"
                else [],
            },
        )
        body = client.get("/api/attention").json()
        assert body["counts"].get("plan_approval_wait") == 1
        assert body["counts"].get("human_merge_wait") == 1

        plan_item = next(i for i in body["items"] if i["kind"] == "plan_approval_wait")
        assert plan_item["source"] == {"task_id": 101}
        assert plan_item["severity"] == "high"
        assert plan_item["reason"] == "plan is waiting for approval"

        merge_item = next(i for i in body["items"] if i["kind"] == "human_merge_wait")
        assert merge_item["source"] == {"task_id": 102}
        assert merge_item["severity"] == "high"
        assert merge_item["reason"] == "task is waiting for human merge"

    def test_attention_surfaces_blocked_tasks(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 201,
                        "title": "stuck hardware task",
                        "status": "blocked",
                        "updated_at": "2026-08-19T01:00:00+00:00",
                    }
                ]
                if kwargs.get("status") == "blocked"
                else [],
            },
        )
        body = client.get("/api/attention").json()
        assert body["counts"].get("blocked_task") == 1
        blocked_item = next(i for i in body["items"] if i["kind"] == "blocked_task")
        assert blocked_item["source"] == {"task_id": 201}
        assert blocked_item["severity"] == "medium"
        assert "is blocked" in blocked_item["reason"]

    def test_attention_explains_parked_blocker_and_decision_owner(self, client, monkeypatch):
        monkeypatch.setenv("UA_OPERATOR_NAME", "Riley")
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 451,
                        "title": "Browser WebGPU verification",
                        "status": "blocked",
                        "notes": (
                            "decision-owner: Riley\n"
                            "unblock-condition: one human-facing slice independently verified end-to-end"
                        ),
                        "updated_at": "2026-08-19T09:00:00+00:00",
                    }
                ]
                if kwargs.get("status") == "blocked"
                else [],
            },
        )
        body = client.get("/api/attention").json()

        item = next(i for i in body["items"] if i["source"] == {"task_id": 451})
        assert item["decision_type"] == "the operator's decision"
        assert "human-facing slice" in item["reason"]

    def test_attention_surfaces_executor_degraded(self, client, monkeypatch):
        # system_doctor's real contract (tools/doctor.py) is uppercase
        # LIVE/DORMANT/MISCONFIGURED/UNKNOWN — a prior version of this test
        # used the non-contract value "fail", which hid a regression where the
        # attention item's status check compared against lowercase values that
        # system_doctor never emits (task #445 / PR #197 review).
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "system_doctor",
            lambda **kwargs: {
                "summary": {"live": 0, "dormant": 0, "misconfigured": 1, "unknown": 0},
                "checks": [
                    {
                        "capability": "active_executor_service",
                        "status": "MISCONFIGURED",
                        "message": "unified-agent.service is inactive",
                    }
                ],
            },
        )
        # Clear doctor cache to force fresh query
        from unified_agent.webui import api

        api._doctor_cache = None

        body = client.get("/api/attention").json()
        assert body["counts"].get("executor_degraded") == 1
        exec_item = next(i for i in body["items"] if i["kind"] == "executor_degraded")
        assert exec_item["severity"] == "high"
        assert "inactive" in exec_item["reason"]

    def test_attention_surfaces_executor_health_query_failure(self, client, monkeypatch):
        from unified_agent.webui import api

        async def failed_health(force=False):
            return {"checks": [], "summary": {}, "error": "system_doctor failed: timeout"}

        monkeypatch.setattr(api, "health_payload", failed_health)

        body = client.get("/api/attention").json()

        assert "system_doctor failed: timeout" in body["errors"]

    def test_digest_uses_note_owner_fallback_with_evidence(self, client, monkeypatch):
        from unified_agent.webui import api

        async def clean_attention():
            return {"items": [], "counts": {}, "total": 0, "errors": [], "omitted": []}

        async def agents():
            return {"agents": [{"label": "Desktop_agent3", "status": "working"}]}

        async def sessions(limit=100):
            return {"sessions": []}

        async def cycles(limit=50):
            return {"cycles": []}

        async def health(force=False):
            return {"summary": {}, "checks": []}

        monkeypatch.setattr(api, "attention_payload", clean_attention)
        monkeypatch.setattr(api, "agents_payload", agents)
        monkeypatch.setattr(api, "sessions_payload", sessions)
        monkeypatch.setattr(api, "cycles_payload", cycles)
        monkeypatch.setattr(api, "health_payload", health)
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 302,
                        "title": "note-owned lane",
                        "status": "doing",
                        "claimed_by": None,
                        "notes": "handoff\n\nclaimed-by: Desktop_agent3",
                        "project": "unified-agent",
                        "worktree_path": ".worktrees/task-302",
                        "branch": "task/302",
                    }
                ]
                if kwargs.get("status") == "doing"
                else []
            },
        )

        body = client.get("/api/digest").json()

        task = body["running"]["tasks"][0]
        # Display still surfaces the notes-derived hint...
        assert task["claimed_by"] == "Desktop_agent3"
        assert task["claimed_by_source"] == "notes_claimed_by"
        # ...but lane ownership counts only the structured claimed_by column.
        # A `claimed-by:` note is descriptive and can outlive claim
        # expiry/release, so treating it as a current claim can point Operator
        # at the wrong agent and inflate that agent's active-task count
        # (task #445 / PR #197 review). This task's claimed_by is NULL, so it
        # must not appear as Desktop_agent3's active task.
        assert body["lane_ownership"][0]["active_task"] is None
        assert body["lane_ownership"][0]["active_tasks_count"] == 0

    def test_digest_reports_doing_and_done_query_failures(self, client, monkeypatch):
        from unified_agent.webui import api

        async def clean_attention():
            return {"items": [], "counts": {}, "total": 0, "errors": [], "omitted": []}

        async def empty_agents():
            return {"agents": []}

        async def empty_sessions(limit=100):
            return {"sessions": []}

        async def empty_cycles(limit=50):
            return {"cycles": []}

        async def clean_health(force=False):
            return {"summary": {}, "checks": []}

        monkeypatch.setattr(api, "attention_payload", clean_attention)
        monkeypatch.setattr(api, "agents_payload", empty_agents)
        monkeypatch.setattr(api, "sessions_payload", empty_sessions)
        monkeypatch.setattr(api, "cycles_payload", empty_cycles)
        monkeypatch.setattr(api, "health_payload", clean_health)
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [],
                "error": f"task_list failed: {kwargs['status']} query",
            },
        )

        body = client.get("/api/digest").json()

        assert body["status"] == "unavailable"
        assert "task_list failed: doing query" in body["errors"]
        assert "task_list failed: done query" in body["errors"]

    def test_digest_answers_five_facets(self, client, monkeypatch):
        from datetime import UTC, datetime

        # This complete-data scenario opts into the now-unconfigured chat integration.
        monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.com")

        now_iso = datetime.now(UTC).isoformat()
        # Mock active tasks and doing tasks
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_list",
            lambda **kwargs: {
                "tasks": [
                    {
                        "id": 301,
                        "title": "doing lane A task",
                        "status": "doing",
                        "claimed_by": "Desktop_agent3",
                        "project": "unified-agent",
                        "worktree_path": ".worktrees/task-301",
                        "branch": "task/301",
                        "updated_at": now_iso,
                    }
                ]
                if kwargs.get("status") == "doing"
                else [
                    {
                        "id": 300,
                        "title": "just finished task",
                        "status": "done",
                        "completed_at": now_iso,
                        "claimed_by": "Desktop_Agent1",
                        "project": "unified-agent",
                    }
                ]
                if kwargs.get("status") == "done"
                else [],
            },
        )
        from unified_agent.webui import api

        api._doctor_cache = None

        body = client.get("/api/digest").json()
        assert body["errors"] == []
        # 1. Running facet
        assert body["running"]["total"] >= 1
        assert body["running"]["tasks"][0]["id"] == 301
        assert body["running"]["tasks"][0]["claimed_by"] == "Desktop_agent3"

        # 2. Changed today facet
        assert body["changed_today"]["total_completed"] >= 1
        assert body["changed_today"]["tasks_completed"][0]["id"] == 300

        # 3. Lane ownership facet
        assert any(
            lane["agent"] == "Desktop_agent3" and lane["active_task"] is not None
            for lane in body["lane_ownership"]
        )

        # 4. Status and evidence
        assert body["status"] in ("clean", "action_needed", "degraded", "error")
        assert "running" in body["evidence"]
        assert "completed today" in body["evidence"]

        # 5. Scanned sources
        assert "tasks" in body["scanned_sources"]
        assert "executor_health" in body["scanned_sources"]


class TestCrewLive:
    """FirstMate-style session watch + intervene (task #1025)."""

    def test_crew_live_unknown_session_is_404(self, client):
        assert client.get("/api/sessions/nosuchid/crew").status_code == 404

    def test_crew_intervene_requires_auth(self, client):
        res = client.post(
            "/api/action/crew-intervene",
            json={"session_id": "x", "body": "hi"},
        )
        assert res.status_code == 401

    def test_crew_intervene_from_operator(self, client, tmp_path, monkeypatch):
        from unified_agent import registry

        monkeypatch.setenv(
            "UA_AGENT_TOKENS",
            f"Desktop_Agent1:{TOKEN},Desktop_Person:operator-tok",
        )
        worktree = tmp_path / "crew-wt-op"
        worktree.mkdir()
        sid = registry.start_session(
            "Desktop_Agent1",
            "pi_run",
            worktree_path=str(worktree),
        )
        assert sid

        live = client.get(f"/api/sessions/{sid}/crew")
        assert live.status_code == 200
        assert live.json()["live"] is True
        assert live.json()["messages"] == []

        res = client.post(
            "/api/action/crew-intervene",
            headers={"Authorization": "Bearer operator-tok"},
            json={
                "session_id": sid,
                "body": "stay on the failing assertion",
                "message_id": "01CREWWEB0000000000000000001",
            },
        )
        assert res.status_code == 202
        payload = res.json()
        assert payload["accepted"] is True
        assert payload["from_agent"] == "Operator"
        assert payload["to_agent"] == "Desktop_Agent1"
        assert payload["worktree_written"] is True

        live = client.get(f"/api/sessions/{sid}/crew").json()
        assert len(live["messages"]) == 1
        assert live["messages"][0]["body"] == "stay on the failing assertion"
        drop = worktree / ".ua" / "interventions.jsonl"
        assert drop.is_file()

    def test_crew_intervene_rejects_self(self, client):
        from unified_agent import registry

        sid = registry.start_session("Desktop_Agent1", "pi_run")
        assert sid
        res = client.post(
            "/api/action/crew-intervene",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"session_id": sid, "body": "cannot nudge myself"},
        )
        assert res.status_code == 400
        assert "must differ" in res.json()["error"]


class TestCloudPanel:
    """Control Center GCP health / instance / budget panel (task #1044).

    The panel is read-first: it calls gcloud_inventory only, and must never
    present an unreadable project as an idle one. Each test pins one of the
    states an operator has to be able to tell apart.
    """

    @pytest.fixture(autouse=True)
    def _clear_cloud_cache(self, monkeypatch):
        monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
        monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
        from unified_agent.webui import api as webui_api

        webui_api._cloud_cache = None
        yield
        webui_api._cloud_cache = None

    @staticmethod
    def _patch_inventory(monkeypatch, result):
        from unified_agent.webui import api as webui_api

        async def fake_call_tool(name, /, **kwargs):
            if name == "gcloud_inventory":
                return result
            raise AssertionError(f"cloud panel must not call {name!r}")

        monkeypatch.setattr(webui_api, "call_tool", fake_call_tool)

    def test_healthy_running_instance(self, client, monkeypatch):
        self._patch_inventory(
            monkeypatch,
            {
                "status": "ok",
                "project_id": "ua-worker",
                "sections": {"instances": "ok", "buckets": "ok", "budgets": "ok"},
                "instances": [
                    {"name": "example-worker", "status": "RUNNING", "zone": "us-central1-a"}
                ],
                "buckets": [{"name": "ua-worker-backups-107", "location": "US"}],
                "budgets": [{"displayName": "ua-worker-10"}],
                "errors": [],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["status"] == "ok"
        assert body["instance_state"] == "running"
        assert body["budgets"]["state"] == "ok"
        assert body["backups"]["state"] == "present"
        assert body["errors"] == []
        assert body["remediation"] == "manual"

    def test_unreadable_instances_report_unknown_not_absent(self, client, monkeypatch):
        """The #1043/#1044 invariant: 'could not look' must not render as 'not there'.

        This is the live failure observed against ua-worker -- the hub
        credential could not list instances while the VM was RUNNING. An
        operator reading 'absent' would conclude the worker had been deleted.
        """
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"instances": "unavailable", "buckets": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [{"section": "instances", "error": "PERMISSION_DENIED"}],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["instance_state"] == "unknown"
        assert body["instance_state"] != "absent"
        assert body["status"] == "degraded"
        assert any(e["section"] == "instances" for e in body["errors"])

    def test_genuinely_empty_project_reports_absent(self, client, monkeypatch):
        """The mirror case: a readable project with no instances is 'absent', not 'unknown'."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "ok",
                "project_id": "ua-worker",
                "sections": {"instances": "ok", "buckets": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["instance_state"] == "absent"
        assert body["backups"]["state"] == "none"

    def test_backup_readiness_unknown_when_buckets_unreadable(self, client, monkeypatch):
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"instances": "ok", "buckets": "unavailable"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
                "buckets": [],
                "budgets": [],
                "errors": [{"section": "buckets", "error": "PERMISSION_DENIED"}],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["backups"]["state"] == "unknown"
        assert body["backups"]["buckets"] == []

    def test_tool_failure_degrades_to_unknown_in_band(self, client, monkeypatch):
        """A gcloud_inventory exception must surface as unknown + a named error, not a 500."""
        from unified_agent.webui import api as webui_api

        async def boom(name, /, **kwargs):
            raise RuntimeError("gcloud credentials expired")

        monkeypatch.setattr(webui_api, "call_tool", boom)

        res = client.get("/api/cloud")
        assert res.status_code == 200
        body = res.json()

        assert body["status"] == "unknown"
        assert body["instance_state"] == "unknown"
        assert body["budgets"]["state"] == "unknown"
        assert body["backups"]["state"] == "unknown"
        assert any("gcloud_inventory" in str(e.get("section", "")) for e in body["errors"])

    def test_payload_carries_stale_data_timestamp(self, client, monkeypatch):
        """The pane must be able to stamp how old the reading is."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "ok",
                "project_id": "ua-worker",
                "sections": {"instances": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [],
            },
        )
        first = client.get("/api/cloud").json()
        assert first["checked_at"]
        assert first["cached_age_s"] == 0.0

        # Second read inside the cache window is served from cache and must
        # say so rather than presenting a stale reading as fresh.
        second = client.get("/api/cloud").json()
        assert second["checked_at"] == first["checked_at"]
        assert second["cached_age_s"] >= 0.0

    def test_panel_never_calls_a_mutating_tool(self, client, monkeypatch):
        """No auto-remediation: the panel's only tool call is the read-only inventory."""
        from unified_agent.webui import api as webui_api

        called = []

        async def recording_call_tool(name, /, **kwargs):
            called.append(name)
            return {
                "status": "ok",
                "project_id": "ua-worker",
                "sections": {"instances": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [],
            }

        monkeypatch.setattr(webui_api, "call_tool", recording_call_tool)
        client.get("/api/cloud")

        assert called == ["gcloud_inventory"]
        assert "gcloud_ops" not in called


class TestCloudPanelCoverage:
    """Coverage and credential-honesty cases for the cloud panel (task #1044).

    These pin the distinction the panel exists to make: what was read, what was
    not, and what must therefore not be asserted.
    """

    @pytest.fixture(autouse=True)
    def _clear_cloud_cache(self, monkeypatch):
        monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
        monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
        from unified_agent.webui import api as webui_api

        webui_api._cloud_cache = None
        yield
        webui_api._cloud_cache = None

    @staticmethod
    def _patch_inventory(monkeypatch, result):
        from unified_agent.webui import api as webui_api

        async def fake_call_tool(name, /, **kwargs):
            if name == "gcloud_inventory":
                return result
            raise AssertionError(f"cloud panel must not call {name!r}")

        monkeypatch.setattr(webui_api, "call_tool", fake_call_tool)

    def test_empty_result_with_no_error_under_partial_coverage_is_unknown(
        self, client, monkeypatch
    ):
        """An under-privileged credential's silence is not proof of absence.

        This is the live hub shape: the service account cannot describe the
        project, so an empty instance list must not be reported as "no worker",
        even when the instances section itself claims to have read cleanly.
        """
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"project": "unavailable", "instances": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [{"section": "project", "error": "caller does not have permission"}],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["instance_state"] == "unknown"
        assert body["instance_state"] != "absent"
        assert body["partial_coverage"] is True

    def test_absent_is_claimed_only_on_full_coverage(self, client, monkeypatch):
        """The mirror: with everything readable, zero instances really is absence."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "ok",
                "project_id": "ua-worker",
                "sections": {"project": "ok", "instances": "ok", "buckets": "ok"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["instance_state"] == "absent"
        assert body["partial_coverage"] is False

    def test_partial_inventory_is_surfaced_explicitly(self, client, monkeypatch):
        """Partial coverage is a named field, not something to infer from the lists."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"instances": "ok", "buckets": "unavailable", "budgets": "unavailable"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
                "buckets": [],
                "budgets": [],
                "errors": [
                    {"section": "buckets", "error": "PERMISSION_DENIED"},
                    {"section": "budgets", "error": "billing API not enabled"},
                ],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["partial_coverage"] is True
        assert body["sections"]["buckets"] == "unavailable"
        assert body["status"] == "degraded"
        # A readable RUNNING instance is still reported as running.
        assert body["instance_state"] == "running"
        assert len(body["errors"]) == 2

    def test_unavailable_billing_api_is_unknown_with_a_named_reason(self, client, monkeypatch):
        """The live case: Cloud Billing API is not enabled on the project."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"instances": "ok", "budgets": "unavailable"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
                "buckets": [],
                "budgets": [],
                "errors": [
                    {
                        "section": "budgets",
                        "error": "Cloud Billing API has not been used in project ... or it is disabled",
                    }
                ],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["budgets"]["state"] == "unknown"
        assert body["budgets"]["items"] == []
        reason = next(e for e in body["errors"] if e["section"] == "budgets")["error"]
        assert "Billing API" in reason

    def test_unavailable_hub_credentials_report_error_not_health(self, client, monkeypatch):
        """No usable credential means no health claim at all."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "error",
                "project_id": "ua-worker",
                "sections": {"project": "unavailable", "instances": "unavailable"},
                "instances": [],
                "buckets": [],
                "budgets": [],
                "errors": [
                    {
                        "section": "project",
                        "error": "could not execute gcloud: credentials expired",
                    },
                    {
                        "section": "instances",
                        "error": "could not execute gcloud: credentials expired",
                    },
                ],
            },
        )
        body = client.get("/api/cloud").json()

        assert body["status"] == "error"
        assert body["instance_state"] == "unknown"
        assert body["partial_coverage"] is True
        assert body["backups"]["state"] == "unknown"

    def test_malformed_inventory_response_does_not_read_as_healthy(self, client, monkeypatch):
        """A response missing every expected key must degrade, not default to ok."""
        self._patch_inventory(monkeypatch, {})
        body = client.get("/api/cloud").json()

        assert body["status"] == "unknown"
        assert body["instance_state"] == "unknown"
        assert body["budgets"]["state"] == "unknown"
        assert body["backups"]["state"] == "unknown"

    def test_payload_contains_no_credentials_or_connection_strings(self, client, monkeypatch):
        """Nothing secret-shaped may reach the pane, even via a gcloud error string."""
        self._patch_inventory(
            monkeypatch,
            {
                "status": "degraded",
                "project_id": "ua-worker",
                "sections": {"instances": "ok"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
                "buckets": [],
                "budgets": [],
                "errors": [{"section": "budgets", "error": "billing API not enabled"}],
            },
        )
        raw = client.get("/api/cloud").text.lower()

        for marker in ("bearer ", "ya29.", "private_key", "-----begin", "password=", "token="):
            assert marker not in raw, f"payload leaked {marker!r}"


class TestNodeSessionI3:
    """Sessions pane launcher routes (task #1329)."""

    def test_nodes_empty(self, client):
        res = client.get("/api/nodes", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 200
        body = res.json()
        assert body["nodes"] == []
        assert body["recent_items"] == []

    def test_nodes_lists_registered_device(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        from unified_agent import node_queue

        node_queue.register(
            "desk-ui",
            identity="Desktop_Agent1",
            facts={
                "backends": ["codex"],
                "harnesses": {"acp:codex": {"path": "/bin/codex"}},
                "worktree_roots": [{"path": str(tmp_path), "worktrees": 1}],
                "os": {"system": "Linux"},
            },
            version="test",
        )
        res = client.get("/api/nodes", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 200
        nodes = res.json()["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node"] == "desk-ui"
        assert nodes[0]["backends"] == ["codex"]
        assert "acp:codex" in nodes[0]["harnesses"]
        assert str(tmp_path) in nodes[0]["worktree_roots"]

    def test_node_session_start_and_cancel(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        monkeypatch.setenv(
            "UA_AGENT_TOKENS",
            f"Desktop_Agent1:{TOKEN},Desktop_Person:operator-tok",
        )
        from unified_agent import config, node_queue
        from unified_agent.tools.approvals import approval_resolve

        node_queue.register(
            "desk-ui",
            identity="Desktop_Agent1",
            facts={
                "backends": ["codex"],
                "harnesses": {},
                "worktree_roots": [{"path": str(tmp_path), "worktrees": 0}],
            },
            version="test",
        )
        body = {
            "action": "start",
            "node": "desk-ui",
            "prompt": "ship it",
            "cwd": str(tmp_path),
            "backend": "codex",
        }
        pending = client.post(
            "/api/action/node-session",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert pending.status_code == 202
        approval_id = pending.json()["approval_id"]

        token = config.agent_identity_var.set("Desktop_Person")
        try:
            approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(token)

        queued = client.post(
            "/api/action/node-session",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert queued.status_code == 201
        item_id = queued.json()["item_id"]
        assert queued.json()["status"] == "queued"

        recent = client.get("/api/nodes", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert any(i["id"] == item_id for i in recent["recent_items"])

        cancelled = client.post(
            "/api/action/node-session",
            json={"action": "cancel", "item_id": item_id},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True


async def test_retired_unconfigured_zulip_is_not_an_attention_error(monkeypatch):
    """#946 removed the built-in Zulip site: a retired, unconfigured Zulip is no
    source to scan, while an un-retired Zulip with no site stays an error."""
    from datetime import UTC, datetime

    from unified_agent.webui import api

    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
    assert await api._zulip_attention_items(datetime.now(UTC)) == ([], [], [])

    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    _, _, errors = await api._zulip_attention_items(datetime.now(UTC))
    assert errors and "UA_ZULIP_SITE" in errors[0]

    # An explicitly set but invalid site is reported even while retired.
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
    monkeypatch.setenv("UA_ZULIP_SITE", "http://zulip.example.test")
    _, _, errors = await api._zulip_attention_items(datetime.now(UTC))
    assert errors and "UA_ZULIP_SITE" in errors[0]
