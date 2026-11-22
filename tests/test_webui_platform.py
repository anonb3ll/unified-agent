"""Read-only platform HTTP/JSON surface (task #1071).

These tests pin the observable contract the card's DoD names: the manifest
over HTTP is exactly ``object_model.manifest()`` (asserted, not eyeballed);
every ``served=True`` object type is readable by kind+id over HTTP and
returns the same payload the MCP resource path resolves for the same id;
a profile denied the object's declared ``profile_tool`` is denied over HTTP
too; unknown ids and unknown kinds are 4xx, never 500.

The MCP payload comparison is deliberate: the HTTP route calls the registry's
own reader (``object_type.reader()``) with the same id the MCP resource
template extracts, so byte-identical output is the point of the seam — not a
second hand-maintained contract.
"""

from __future__ import annotations

import json
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    config,
    mcp_live_state,
    object_model,
    platform_readers,
    profiles,
    server,
    storage,
    webui,
)
from unified_agent.tools.tasks import task_add

TOKEN_CLAUDE = f"tok-claude-{secrets.token_urlsafe(8)}"
TOKEN_GROK = f"tok-grok-{secrets.token_urlsafe(8)}"
TOKEN_OPERATOR = f"tok-operator-{secrets.token_urlsafe(8)}"

_ULID = "01J8ZQ4T7X9K2M5N7P8R0S3T6W"


@pytest.fixture
def client(monkeypatch):
    """In-process TestClient only — never the live hub.

    Clear hub-client env so a host ``UA_HTTP_HUB_URL`` / executor token cannot
    turn fixture teardown or a doctor probe into an unauthenticated round-trip
    against the real HTTP hub (task #1127 verification).
    """
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        ",".join(
            [
                f"Desktop_Agent1:{TOKEN_CLAUDE}",
                f"Desktop_Agent8:{TOKEN_GROK}",
                f"Desktop_Person:{TOKEN_OPERATOR}",
            ]
        ),
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _insert_approval(*, status: str = "pending") -> int:
    conn = storage.connect()
    try:
        cursor = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "run_cmd",
                '{"command": "echo hi"}',
                "Desktop_Agent1",
                status,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_event(*, payload: str = '{"k":"v"}') -> str:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, created_at, prev_hash, entry_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _ULID,
                "agent.task.updated",
                payload,
                "Desktop_Agent5",
                "2026-09-17T00:00:00Z",
                "prev-deadbeef",
                "entry-cafebabe",
            ),
        )
        conn.commit()
        return _ULID
    finally:
        conn.close()


def _insert_run(run_id: str = "run-webui-001") -> str:
    """Seed a dispatch_runs row with a real backing task row (FK constraint)."""
    tid = task_add("platform run backing task")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tid,
                "Desktop_agent3",
                "running",
                "test",
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:00:00Z",
            ),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


class TestManifest:
    def test_manifest_is_exactly_object_model_manifest(self, client):
        res = client.get("/api/platform/manifest", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 200, res.text
        expected = object_model.manifest()
        assert res.json() == expected
        # JSON round-trip parity too: the wire bytes decode to the same model.
        assert json.loads(res.text) == expected

    def test_manifest_carries_the_full_registry(self, client):
        res = client.get("/api/platform/manifest", headers=_auth(TOKEN_CLAUDE))
        body = res.json()
        assert body["schema_version"] == object_model.MANIFEST_SCHEMA_VERSION
        kinds = [entry["kind"] for entry in body["object_types"]]
        assert len(kinds) == len(object_model.OBJECT_TYPES)
        served = {entry["kind"] for entry in body["object_types"] if entry["served"]}
        # Task #1072 served every remaining declared type; the manifest must
        # report the same surface the registry does (all ten, none aspirational).
        assert served == {ot.kind for ot in object_model.OBJECT_TYPES}

    def test_manifest_requires_auth_when_enforced(self, client):
        assert client.get("/api/platform/manifest").status_code == 401

    def test_adding_an_eleventh_kind_appears_in_the_http_manifest(self, client, monkeypatch):
        """Divergence pin for the Control Center Platform pane (task #1127).

        The pane must render whatever ``object_model.manifest()`` serves — no
        hardcoded kind list. Extending the live registry with a synthetic 11th
        kind must surface on GET /api/platform/manifest without route stubs.

        Patch ``OBJECT_TYPES`` and ``_BY_KIND`` together so lookup helpers stay
        consistent for the duration of the test; monkeypatch restores both.
        """
        from dataclasses import replace

        extra = replace(
            object_model.by_kind("task"),
            kind="widget",
            uri_template="unified-agent://widgets/{widget_id}",
            name="widget",
            title="Widget",
            description="Synthetic 11th kind for the Platform pane divergence pin.",
            id_param="widget_id",
        )
        extended = (*object_model.OBJECT_TYPES, extra)
        monkeypatch.setattr(object_model, "OBJECT_TYPES", extended)
        monkeypatch.setattr(
            object_model,
            "_BY_KIND",
            {object_type.kind: object_type for object_type in extended},
        )
        res = client.get("/api/platform/manifest", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 200, res.text
        body = res.json()
        kinds = [entry["kind"] for entry in body["object_types"]]
        assert "widget" in kinds
        assert body == object_model.manifest()
        assert len(kinds) == len(object_model.OBJECT_TYPES)


class TestObjectReads:
    """Every served type over HTTP must equal the MCP payload for the same id."""

    def test_task_read_matches_mcp_payload(self, client):
        tid = task_add("platform web read")["id"]
        res = client.get(f"/api/platform/objects/task/{tid}", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 200, res.text
        assert res.json() == mcp_live_state.read_task(str(tid))

    def test_run_read_matches_mcp_payload(self, client):
        rid = _insert_run()
        res = client.get(f"/api/platform/objects/run/{rid}", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 200, res.text
        assert res.json() == mcp_live_state.read_run(rid)

    def test_approval_read_matches_mcp_payload(self, client):
        approval_id = _insert_approval()
        res = client.get(
            f"/api/platform/objects/approval/{approval_id}", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 200, res.text
        assert res.json() == platform_readers.read_approval(str(approval_id))

    def test_evidence_event_read_matches_mcp_payload(self, client):
        _insert_event()
        res = client.get(
            f"/api/platform/objects/evidence_event/{_ULID}", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 200, res.text
        assert res.json() == platform_readers.read_evidence_event(_ULID)

    def test_board_read_matches_mcp_payload(self, client):
        task_add("platform board read")
        res = client.get("/api/platform/objects/board", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 200, res.text
        assert res.json() == mcp_live_state.read_board()


class TestProfileGate:
    """The HTTP plane must not be a permission bypass: same gate as MCP."""

    def test_research_profile_is_denied_approval_over_http(self, client):
        approval_id = _insert_approval()
        res = client.get(f"/api/platform/objects/approval/{approval_id}", headers=_auth(TOKEN_GROK))
        assert res.status_code == 403, res.text
        assert "not exposed" in res.json()["error"]

    def test_research_profile_is_denied_run_over_http(self, client):
        rid = _insert_run()
        res = client.get(f"/api/platform/objects/run/{rid}", headers=_auth(TOKEN_GROK))
        assert res.status_code == 403, res.text

    def test_operator_profile_is_denied_run_over_http(self, client):
        rid = _insert_run()
        res = client.get(f"/api/platform/objects/run/{rid}", headers=_auth(TOKEN_OPERATOR))
        assert res.status_code == 403, res.text

    def test_research_profile_can_still_read_evidence(self, client):
        """Gate granularity: research allows event_log_query, denies approval_pending."""
        _insert_event()
        res = client.get(f"/api/platform/objects/evidence_event/{_ULID}", headers=_auth(TOKEN_GROK))
        assert res.status_code == 200, res.text

    def test_http_denial_agrees_with_the_mcp_visibility_rule(self, client):
        """The same declared profile_tool drives both planes — prove it directly."""
        _insert_run()
        token = config.agent_identity_var.set("Desktop_Agent8")
        session = config.mcp_session_id_var.set("platform-http-gate-parity")
        try:
            assert mcp_live_state.resource_visible("unified-agent://runs/{run_id}") is False
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(token)
            with profiles._lock:
                profiles._active.pop("platform-http-gate-parity", None)
        res = client.get("/api/platform/objects/run/run-webui-001", headers=_auth(TOKEN_GROK))
        assert res.status_code == 403, res.text


class TestNotFoundAndBadRequest:
    def test_unknown_kind_is_clear_4xx(self, client):
        for path in ("/api/platform/objects/unicorn", "/api/platform/objects/unicorn/1"):
            res = client.get(path, headers=_auth(TOKEN_CLAUDE))
            assert res.status_code == 404, res.text
            assert "error" in res.json()

    def test_served_kind_with_missing_id_is_4xx_not_500(self, client):
        # memory_note is served (task #1072) and readable by the developer
        # profile, so a missing note must hit the established not-found error
        # over HTTP — never a 500.
        res = client.get("/api/platform/objects/memory_note/p-missing", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 404, res.text
        assert "unknown memory note" in res.json()["error"]

    def test_developer_over_http_is_denied_legacy_gated_objects(self, client):
        """plan/delegation/agent gates are legacy tool names denied on HTTP.

        The declared profile_tool for these objects is the legacy tool name
        (plan_get etc.), which authenticated HTTP developer sessions do not
        expose — they read through the consolidated manager instead. The HTTP
        plane must NOT be a permission bypass: deny here too.
        """
        res = client.get("/api/platform/objects/plan/p-1", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 403, res.text
        assert "not exposed" in res.json()["error"]

    def test_declared_but_unserved_kind_is_4xx_not_500(self, client, monkeypatch):
        """The ObjectNotServed branch survives as a defensive path.

        The real registry has no unserved types anymore, so the branch is
        exercised with a deliberately flipped type to prove it still degrades
        to a clean 4xx rather than a 500. The flip patches the registry lookup
        itself (``object_model.get``), because ``read_object`` resolves kinds
        through that entry point.
        """
        from dataclasses import replace

        plan = replace(object_model.by_kind("plan"), served=False, reader_path=None)
        monkeypatch.setattr(object_model, "get", lambda kind: plan if kind == "plan" else None)
        res = client.get("/api/platform/objects/plan/p-1", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 404, res.text
        assert "not served" in res.json()["error"]

    def test_unknown_id_is_not_found(self, client):
        res = client.get("/api/platform/objects/task/999999", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 404, res.text

    def test_malformed_id_is_not_found_like_mcp(self, client):
        res = client.get("/api/platform/objects/task/not-an-id", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 404, res.text

    def test_collection_kind_rejects_an_id(self, client):
        res = client.get("/api/platform/objects/board/extra", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 400, res.text

    def test_id_addressed_kind_requires_an_id(self, client):
        res = client.get("/api/platform/objects/task", headers=_auth(TOKEN_CLAUDE))
        assert res.status_code == 400, res.text

    def test_objects_require_auth_when_enforced(self, client):
        assert client.get("/api/platform/objects/task/1").status_code == 401
        assert client.get("/api/platform/objects/board").status_code == 401

    def test_error_bodies_are_bounded_json(self, client):
        res = client.get("/api/platform/objects/unicorn", headers=_auth(TOKEN_CLAUDE))
        assert res.headers["content-type"].startswith("application/json")
        assert isinstance(res.json().get("error"), str)


class TestProvenanceGraphRoute:
    """Task #1287: /api/platform/provenance-graph, alongside /api/platform/objects."""

    def test_requires_auth_when_enforced(self, client):
        assert client.get("/api/platform/provenance-graph").status_code == 401

    def test_default_scope_returns_a_graph_shape(self, client):
        res = client.get(
            "/api/platform/provenance-graph?task_id=999999", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["node_count"] == 0
        assert body["edge_count"] == 0
        assert "canonical_hash" in body
        assert body["scope"]["task_id"] == 999999

    def test_invalid_task_id_is_400(self, client):
        res = client.get(
            "/api/platform/provenance-graph?task_id=not-a-number", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 400, res.text

    def test_by_human_requires_human_param(self, client):
        res = client.get(
            "/api/platform/provenance-graph?query=by_human", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 400, res.text
        assert "by_human" in res.json()["error"]

    def test_by_release_requires_task_id_or_task_ids(self, client):
        res = client.get(
            "/api/platform/provenance-graph?query=by_release", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 400, res.text
        assert "by_release" in res.json()["error"]

    def test_unknown_query_is_400(self, client):
        res = client.get(
            "/api/platform/provenance-graph?query=nonsense", headers=_auth(TOKEN_CLAUDE)
        )
        assert res.status_code == 400, res.text

    def test_by_release_reads_named_task_ids(self, client):
        tid = task_add("provenance graph release task")["id"]
        res = client.get(
            f"/api/platform/provenance-graph?query=by_release&task_ids={tid}",
            headers=_auth(TOKEN_CLAUDE),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert f"task:{tid}" in {n["id"] for n in body["nodes"]}
        assert body["scope"]["kind"] == "by_release"

    def test_research_profile_can_read_the_provenance_graph(self, client):
        """Same gate as evidence_event (event_log_query) — research can read it."""
        res = client.get("/api/platform/provenance-graph?task_id=999999", headers=_auth(TOKEN_GROK))
        assert res.status_code == 200, res.text
