"""Vault pane Control Center routes (task #1123).

Ground truth is the real vault tools against a temp vault fixture — search,
page+backlinks, ingest status, and briefing digest are asserted through the
HTTP surface, not stubs. The read routes never reach vault_write or mark an
ingest. The only web vault writes are the two pinned Control Center routes
(task #2469), confined to the write dirs and refusing locked notes.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.tools.vault import vault_write
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-vault-{secrets.token_urlsafe(8)}"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """PARA-style vault isolated under tmp_path (mirrors tests/test_vault.py)."""
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "01 - Projects", "Logs", "Wiki"):
        (root / d).mkdir(parents=True)
    (root / "01 - Projects" / "hermes.md").write_text(
        "# Hermes\n\nSupervisor persona. See [[unified-agent]].\n"
    )
    (root / "01 - Projects" / "unified-agent.md").write_text(
        "# unified-agent\n\nTool server. Linked from [[hermes]].\n"
    )
    (root / "00 - Inbox" / "fresh.md").write_text("# Fresh capture\n")
    (root / "00 - Inbox" / "blank.md").write_text("   \n")
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    return root


@pytest.fixture
def client(monkeypatch, vault):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestVaultSearchRoute:
    def test_search_returns_real_matches(self, client):
        res = client.get("/api/vault/search", params={"q": "Supervisor"}, headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert "matches" in body
        assert any("hermes.md" in m["path"] for m in body["matches"])
        assert body.get("error") in (None, "")

    def test_search_requires_auth(self, client):
        assert client.get("/api/vault/search", params={"q": "x"}).status_code == 401

    def test_search_requires_query(self, client):
        res = client.get("/api/vault/search", headers=_auth())
        assert res.status_code == 400, res.text
        assert res.json()["error"]


class TestVaultPageRoute:
    def test_page_renders_content_and_backlinks(self, client):
        res = client.get(
            "/api/vault/page",
            params={"path": "01 - Projects/unified-agent.md"},
            headers=_auth(),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["path"] == "01 - Projects/unified-agent.md"
        assert "Tool server" in body["content"]
        assert "backlinks" in body
        assert any("hermes.md" in b for b in body["backlinks"])
        assert "outgoing" in body

    def test_page_unknown_note_is_404(self, client):
        res = client.get(
            "/api/vault/page",
            params={"path": "01 - Projects/missing.md"},
            headers=_auth(),
        )
        assert res.status_code == 404, res.text
        assert res.json()["error"]


class TestVaultIngestStatusRoute:
    def test_ingest_status_classifies_new_and_empty(self, client, vault):
        fresh = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/fresh.md"},
            headers=_auth(),
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["status"] == "new"

        blank = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/blank.md"},
            headers=_auth(),
        )
        assert blank.status_code == 200, blank.text
        assert blank.json()["status"] == "empty"

        # Mark via the real tool (not the web surface), then re-check unchanged/changed.
        vault_write("00 - Inbox/marked.md", "# Marked\n")
        from unified_agent.tools.vault import vault_ingest_status

        vault_ingest_status("00 - Inbox/marked.md", mark=True, agent="Desktop_Agent1")
        unchanged = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/marked.md"},
            headers=_auth(),
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["status"] == "unchanged"

        (vault / "00 - Inbox" / "marked.md").write_text("# Marked — edited\n")
        changed = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/marked.md"},
            headers=_auth(),
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "changed"


class TestVaultBriefingRoute:
    def test_briefing_includes_inbox_ingest_strip(self, client, vault):
        day = "2026-09-18"
        (vault / "Logs" / f"{day}.md").write_text(
            "---\ndate: 2026-09-18\n---\n\n- **12:00** [Desktop_Agent1] shipped vault pane #1123\n"
        )
        res = client.get("/api/vault/briefing", params={"date": day}, headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["date"] == day
        assert any("vault pane" in e.get("text", "") for e in body["entries"])
        assert "inbox" in body
        statuses = {row["status"] for row in body["inbox"]}
        assert "new" in statuses
        assert "empty" in statuses


class TestVaultNotConfigured:
    def test_named_vault_not_configured_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_VAULT_PATH", "")
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            for path, params in (
                ("/api/vault/search", {"q": "x"}),
                ("/api/vault/page", {"path": "Wiki/Index.md"}),
                ("/api/vault/ingest-status", {"path": "00 - Inbox/x.md"}),
                ("/api/vault/briefing", {}),
            ):
                res = client.get(path, params=params, headers=_auth())
                assert res.status_code == 200, (path, res.text)
                body = res.json()
                assert body["error"] == "vault not configured"
                assert body.get("code") == "vault_not_configured"

    def test_empty_hosted_vault_is_not_a_not_configured_success(self, monkeypatch, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        vault = tmp_path / "empty-vault"
        vault.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))
        monkeypatch.setenv("UA_HOSTED_WORKSPACE_GUARD", "1")
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/vault/search", params={"q": "invoice"}, headers=_auth())
        assert res.status_code == 400, res.text
        body = res.json()
        assert body.get("code") != "vault_not_configured"
        assert "vault" in body["error"].lower()


class TestVaultReadOnlyInvariant:
    #: The only web vault writes (task #2469, epic #2466: the operator approved vault
    #: writing from the Control Center). A third one must fail this invariant.
    VAULT_WRITE_ROUTES = {
        "/api/action/vault/write": "vault_write",
        "/api/action/vault/journal": "vault_journal",
    }

    def test_vault_reads_are_get_only_and_writes_are_the_two_pinned_routes(self):
        from unified_agent.webui import route_scopes

        # Every registration, not the last one per path: a second route object on
        # an existing path (a POST beside a GET) must not hide behind the first.
        routes_by_path: dict[str, frozenset[str]] = {}
        for route in web_routes():
            if "/vault" not in route.path:
                continue
            methods = frozenset(m for m in (route.methods or set()) if m != "HEAD")
            routes_by_path[route.path] = routes_by_path.get(route.path, frozenset()) | methods
        reads = {p: m for p, m in routes_by_path.items() if p.startswith("/api/vault/")}
        assert reads, "expected vault read routes to be registered"
        for path, methods in reads.items():
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        writes = {p: m for p, m in routes_by_path.items() if p not in reads}
        assert set(writes) == set(self.VAULT_WRITE_ROUTES), sorted(writes)
        for path, tool in self.VAULT_WRITE_ROUTES.items():
            assert writes[path] == frozenset({"POST"}), path
            scope = route_scopes.scope_for("POST", path)
            assert scope.access is route_scopes.Access.CAPABILITY, path
            assert scope.csrf and scope.tools == (tool,), path
        # No vault path declares a mutating method outside the two pinned routes.
        for (method, path), _scope in route_scopes.ROUTE_SCOPES.items():
            if "/vault" in path and method not in {"GET", "HEAD"}:
                assert (method, path) in {("POST", p) for p in self.VAULT_WRITE_ROUTES}, (
                    method,
                    path,
                )

    def test_vault_writes_stay_in_the_write_dirs_and_refuse_locked_notes(self, client, vault):
        (vault / "Wiki" / "locked.md").write_text("---\nlocked: true\n---\nkeep\n")
        for path, body in (
            ("/api/action/vault/write", {"path": "01 - Projects/hermes.md", "content": "x"}),
            ("/api/action/vault/write", {"path": "Wiki/locked.md", "content": "x"}),
            # The tool's lock override is not reachable from the browser: ``force`` is
            # an unknown key here, so a locked note cannot be clobbered over HTTP.
            (
                "/api/action/vault/write",
                {"path": "Wiki/locked.md", "content": "x", "force": True},
            ),
            (
                "/api/action/vault/write",
                {"path": "Wiki/locked.md", "content": "x", "mode": "append", "force": True},
            ),
        ):
            res = client.post(path, json=body, headers=_auth())
            assert res.status_code == 400, (body, res.text)
        assert (vault / "Wiki" / "locked.md").read_text().endswith("keep\n")
        assert "Supervisor" in (vault / "01 - Projects" / "hermes.md").read_text()
        ok = client.post(
            "/api/action/vault/write",
            json={"path": "Wiki/new.md", "content": "fine"},
            headers=_auth(),
        )
        assert ok.status_code == 200, ok.text

    def test_api_helpers_never_reach_vault_mutations(self):
        helpers = [
            webui_api.vault_search_payload,
            webui_api.vault_page_payload,
            webui_api.vault_ingest_status_payload,
            webui_api.vault_briefing_payload,
        ]
        blob = "\n".join(inspect.getsource(fn) for fn in helpers)
        assert "vault_write" not in blob
        assert "vault_journal" not in blob
        assert "vault_move" not in blob
        assert "mark=True" not in blob
        assert 'mark": True' not in blob
        assert "vault_to_memory_ingest" not in blob

    def test_ingest_status_route_ignores_mark_query(self, client, vault):
        """Even a forged mark= query must not mutate the ledger."""
        before = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/fresh.md", "mark": "true", "agent": "Desktop_Agent1"},
            headers=_auth(),
        )
        assert before.status_code == 200, before.text
        assert before.json()["status"] == "new"
        # Still new — nothing was marked.
        again = client.get(
            "/api/vault/ingest-status",
            params={"path": "00 - Inbox/fresh.md"},
            headers=_auth(),
        )
        assert again.json()["status"] == "new"
        assert config.vault_root() is not None
