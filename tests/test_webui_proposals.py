"""Proposals pane Control Center routes (task #1148).

Ground truth is the real vault + task store — pending proposals are asserted
through the HTTP surface against a temp-vault fixture, not stubs. file_task /
dismiss mutate only via CSRF-gated action routes; nothing in the sleep-pass /
cron surface ever files or dismisses.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools.vault import vault_ingest_status
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-proposals-{secrets.token_urlsafe(8)}"

_SKILL_CLUSTER = """\
---
type: skill-cluster-proposal
generated: 2026-09-18T08:00:03+00:00
applied: false
authority: human-review
untrusted_evidence: true
fingerprint: abc123def456
proposals: 2
---

# Candidate skill outlines (proposal only)

## 1. `claim-before-edit` (score 0.9123)

### Trigger intent

Always claim with task_claim before product edits.

### Proposed scope

- project: `project:unified-agent`
- suggested name: `claim-before-edit`

## 2. `reread-after-format` (score 0.8010)

### Trigger intent

Re-read files after format hooks because they can revert edits.
"""

_SESSION_LEARNING = """\
---
type: learning-proposal
session_id: sess-demo-1
agent: Desktop_Agent1
status: completed
applied: false
authority: human-review
untrusted_evidence: true
fingerprint: feedfacecafe
candidates: 1
---

# Session learning candidates

## Session

- session_id: `sess-demo-1`
- agent: `Desktop_Agent1`

## Candidates

### 1. durable_rule (confidence: 0.70)

- provenance: summary
- agent: `Desktop_Agent1`
- session_id: `sess-demo-1`

Never leave UA_SHELL_ALLOWLIST as a blanket wildcard.
"""

_MALFORMED = """\
---
type: skill-cluster-proposal
generated: 2026-09-17T12:00:00+00:00
applied: false
---

# Broken proposal

## not-a-numbered-cluster

This section does not match the expected numbered cluster heading shape.
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """PARA-style vault with seeded proposal files under 00 - Inbox."""
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "01 - Projects", "Logs", "Wiki"):
        (root / d).mkdir(parents=True)
    inbox = root / "00 - Inbox"
    (inbox / "Skill-Cluster-Proposals.md").write_text(_SKILL_CLUSTER, encoding="utf-8")
    (inbox / "Session-Learning-sess-demo-1.md").write_text(_SESSION_LEARNING, encoding="utf-8")
    (inbox / "unrelated-capture.md").write_text("# Not a proposal\n", encoding="utf-8")
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


def _login(client: TestClient) -> str:
    res = client.post("/api/session", json={"token": TOKEN})
    assert res.status_code == 200, res.text
    return res.json()["csrf"]


def _task_row(task_id: int) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT id, title, notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


class TestProposalsReadRoute:
    def test_pending_proposals_render_content_and_source_date(self, client):
        res = client.get("/api/proposals", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        assert body.get("code") in (None, "")
        proposals = body["proposals"]
        assert len(proposals) >= 3  # 2 skill clusters + 1 session learning

        by_title = {p.get("title"): p for p in proposals if not p.get("error")}
        claim = by_title.get("claim-before-edit")
        assert claim is not None
        assert "task_claim" in claim["content"]
        assert claim["source_date"]
        assert "2026-09-18" in claim["source_date"]
        assert claim["path"] == "00 - Inbox/Skill-Cluster-Proposals.md"
        assert claim["kind"] == "skill-cluster-proposal"

        session = next(p for p in proposals if p.get("kind") == "learning-proposal")
        assert "UA_SHELL_ALLOWLIST" in session["content"]
        assert session["path"] == "00 - Inbox/Session-Learning-sess-demo-1.md"
        assert "unrelated-capture" not in " ".join(p.get("path", "") for p in proposals)

    def test_requires_auth(self, client):
        assert client.get("/api/proposals").status_code == 401

    def test_malformed_proposal_is_per_item_error_not_500(self, client, vault):
        (vault / "00 - Inbox" / "Skill-Cluster-Broken.md").write_text(_MALFORMED, encoding="utf-8")
        # Also inject a bad section into the main skill-cluster file.
        (vault / "00 - Inbox" / "Skill-Cluster-Proposals.md").write_text(
            _SKILL_CLUSTER + "\n## broken line without backticks\n\norphan content\n",
            encoding="utf-8",
        )
        res = client.get("/api/proposals", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        errored = [p for p in body["proposals"] if p.get("error")]
        assert errored, "expected at least one per-item parse error"
        for item in errored:
            assert item["error"]
            assert item.get("path")


class TestProposalsNoSource:
    def test_named_no_proposals_source_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_VAULT_PATH", "")
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/proposals", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "no proposals source"
            assert body["code"] == "no_proposals_source"
            assert body["proposals"] == []


class TestProposalFileTask:
    def test_file_task_creates_hub_task_with_attribution(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(p for p in listed if p.get("title") == "claim-before-edit")

        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "file_task", "proposal_id": target["id"]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["applied"] is True
        assert body["action"] == "file_task"
        task_id = body["task_id"]
        assert isinstance(task_id, int)

        row = _task_row(task_id)
        assert "claim" in row["title"].lower() or "claim-before-edit" in row["title"]
        assert "task_claim" in row["notes"]
        assert "00 - Inbox/Skill-Cluster-Proposals.md" in row["notes"]
        assert "source:" in row["notes"].lower() or "Source:" in row["notes"]

    def test_cookie_post_requires_csrf(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(p for p in listed if not p.get("error"))
        csrf = _login(client)

        missing = client.post(
            "/api/action/proposal",
            json={"action": "file_task", "proposal_id": target["id"]},
        )
        assert missing.status_code == 403, missing.text
        assert "CSRF" in missing.json()["error"]

        ok = client.post(
            "/api/action/proposal",
            headers={"x-ua-csrf": csrf},
            json={"action": "file_task", "proposal_id": target["id"]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["applied"] is True


class TestProposalDismiss:
    def test_dismiss_marks_so_no_longer_pending(self, client, vault):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        skill_items = [
            p
            for p in listed
            if p.get("path") == "00 - Inbox/Skill-Cluster-Proposals.md" and not p.get("error")
        ]
        assert skill_items
        target = skill_items[0]

        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": target["id"]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["applied"] is True
        assert body["action"] == "dismiss"
        assert body.get("status") in ("marked", "unchanged")

        # Ledger shows ingested; proposal list no longer includes that path.
        status = vault_ingest_status("00 - Inbox/Skill-Cluster-Proposals.md", mark=False)
        assert status["status"] == "unchanged"

        after = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        assert not any(p.get("path") == "00 - Inbox/Skill-Cluster-Proposals.md" for p in after)
        # Session learning still pending.
        assert any(p.get("kind") == "learning-proposal" for p in after)


class TestProposalActionGuards:
    def test_action_requires_auth(self, client):
        res = client.post(
            "/api/action/proposal",
            json={"action": "dismiss", "proposal_id": "x"},
        )
        assert res.status_code == 401

    def test_unknown_action_is_400(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        pid = listed[0]["id"]
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "approve", "proposal_id": pid},
        )
        assert res.status_code == 400, res.text
        assert res.json()["error"]

    def test_unknown_proposal_id_is_400(self, client):
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": "missing-id-zzz"},
        )
        assert res.status_code == 400, res.text


class TestProposalDetailPayload:
    """Detail payload (task #1157): source ref + ingest status + filing preview."""

    def test_items_carry_ingest_status_and_filing_preview(self, client):
        proposals = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        by_title = {p.get("title"): p for p in proposals if not p.get("error")}
        claim = by_title["claim-before-edit"]

        assert claim["path"] == "00 - Inbox/Skill-Cluster-Proposals.md"
        assert claim["ingest_status"] == "new"
        preview = claim["filing_preview"]
        assert preview["task_title"] == "[proposal] claim-before-edit"
        assert "task_claim" in preview["task_notes"]
        assert "00 - Inbox/Skill-Cluster-Proposals.md" in preview["task_notes"]
        assert "proposal_id" in preview["task_notes"]

        session = next(p for p in proposals if p.get("kind") == "learning-proposal")
        assert session["ingest_status"] == "new"
        assert session["filing_preview"]["task_title"].startswith("[proposal]")

    def test_malformed_item_has_no_filing_preview(self, client, vault):
        (vault / "00 - Inbox" / "Skill-Cluster-Proposals.md").write_text(
            _SKILL_CLUSTER + "\n## broken line without backticks\n\norphan\n",
            encoding="utf-8",
        )
        proposals = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        errored = [p for p in proposals if p.get("error")]
        assert errored
        for item in errored:
            assert item["filing_preview"] is None
            assert item["ingest_status"] in ("new", "changed")

    def test_detail_is_read_only_derivation(self, client, vault):
        """The detail fields never mutate the source or the ingest ledger."""
        path = vault / "00 - Inbox" / "Skill-Cluster-Proposals.md"
        before = path.read_text(encoding="utf-8")
        client.get("/api/proposals", headers=_auth())
        assert path.read_text(encoding="utf-8") == before
        status = vault_ingest_status("00 - Inbox/Skill-Cluster-Proposals.md", mark=False)
        assert status["status"] == "new"


class TestProposalReasonCapture:
    def test_file_task_persists_reason_with_actor(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(p for p in listed if p.get("title") == "claim-before-edit")
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={
                "action": "file_task",
                "proposal_id": target["id"],
                "reason": "matches the claim-before-code protocol",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["applied"] is True
        assert body["reason"] == "matches the claim-before-code protocol"
        assert body["reason_missing"] is False

        row = _task_row(body["task_id"])
        assert "filed_by: `Desktop_Agent1`" in row["notes"]
        assert "reason: matches the claim-before-code protocol" in row["notes"]

    def test_dismiss_persists_reason_with_actor(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(
            p
            for p in listed
            if p.get("path") == "00 - Inbox/Skill-Cluster-Proposals.md" and not p.get("error")
        )
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={
                "action": "dismiss",
                "proposal_id": target["id"],
                "reason": "duplicate of an existing skill",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["applied"] is True
        assert body["reason"] == "duplicate of an existing skill"
        assert body["reason_missing"] is False

        ledger = storage.ingest_ledger_get("00 - Inbox/Skill-Cluster-Proposals.md")
        assert ledger is not None
        assert ledger["agent"] == "Desktop_Agent1"
        assert ledger["reason"] == "duplicate of an existing skill"

    def test_dismiss_without_reason_flagged_not_blocked(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(p for p in listed if not p.get("error"))
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": target["id"]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["applied"] is True
        assert body["reason"] is None
        assert body["reason_missing"] is True

    def test_file_task_without_reason_flagged_not_blocked(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        target = next(p for p in listed if not p.get("error"))
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "file_task", "proposal_id": target["id"]},
        )
        assert res.status_code == 200, res.text
        assert res.json()["reason_missing"] is True

    def test_reason_must_be_a_string(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        pid = listed[0]["id"]
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": pid, "reason": 42},
        )
        assert res.status_code == 400, res.text
        assert "reason" in res.json()["error"]

    def test_reason_length_capped(self, client):
        listed = client.get("/api/proposals", headers=_auth()).json()["proposals"]
        pid = listed[0]["id"]
        over = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": pid, "reason": "x" * 501},
        )
        assert over.status_code == 400, over.text
        assert "reason" in over.json()["error"]

        ok = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": "dismiss", "proposal_id": pid, "reason": "x" * 500},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["reason"] == "x" * 500


class TestClosedActionMap:
    """Red-tier routing proof (task #1157): the proposal action route cannot
    reach shell / vault-write / merge / deploy authority. Its action map is
    closed over file_task | dismiss; every other verb is a named 400.
    """

    _RED_TIER_VERBS = [
        "run_cmd",
        "shell",
        "exec",
        "spawn",
        "eval",
        "vault_write",
        "fs_write",
        "file_write",
        "git_merge_task",
        "merge",
        "deploy",
        "restart_hub",
        "approve",
        "dismiss_all",
        "file_all",
    ]

    @pytest.mark.parametrize("verb", _RED_TIER_VERBS)
    def test_non_file_dismiss_verbs_are_named_400(self, client, verb):
        res = client.post(
            "/api/action/proposal",
            headers=_auth(),
            json={"action": verb, "proposal_id": "any-id"},
        )
        assert res.status_code == 400, res.text
        error = res.json()["error"]
        assert "file_task" in error and "dismiss" in error

    def test_action_map_is_closed_over_two_verbs(self):
        from unified_agent.webui import api

        assert api.PROPOSAL_ACTIONS == frozenset({"file_task", "dismiss"})

    def test_dispatch_reaches_only_file_task_and_dismiss(self):
        """Assert the negative at the source level: neither the route handler
        nor its api dispatcher can name a red-tier capability."""
        from unified_agent.webui import api, routes

        forbidden = (
            "run_cmd",
            "subprocess",
            "os.system",
            "vault_write",
            "fs_write",
            "git_merge",
            "deploy",
            "eval(",
            "exec(",
            "shell",
        )
        dispatcher = inspect.getsource(api.proposal_action)
        assert "file_proposal_as_task" in dispatcher
        assert "dismiss_proposal" in dispatcher
        for token in forbidden:
            assert token not in dispatcher, token
        handler = inspect.getsource(routes.action_proposal)
        for token in forbidden:
            assert token not in handler, token


class TestNeverAutoApproveContract:
    def test_no_scheduler_registers_proposal_actions(self):
        """file_task / dismiss must never ride sleep-pass, evolve, or cron."""
        from unified_agent import evolve, sleep_pass
        from unified_agent.tools import evolve as evolve_tools
        from unified_agent.tools import learning

        assert "file_task" not in inspect.getsource(sleep_pass)
        assert "file_task" not in inspect.getsource(evolve)
        assert "file_task" not in inspect.getsource(evolve_tools)
        assert "/api/action/proposal" not in inspect.getsource(sleep_pass)
        assert "/api/action/proposal" not in inspect.getsource(evolve)
        assert "proposal_file_task" not in inspect.getsource(sleep_pass)
        assert "proposal_dismiss" not in inspect.getsource(sleep_pass)
        # list_pending_proposals is read-only — no mark=True / task_add.
        if hasattr(learning, "list_pending_proposals"):
            src = inspect.getsource(learning.list_pending_proposals)
            assert "mark=True" not in src
            assert "task_add" not in src
        # Action helpers must never be imported by the sleep-pass module.
        sleep_src = inspect.getsource(sleep_pass)
        assert "file_proposal_as_task" not in sleep_src
        assert "dismiss_proposal" not in sleep_src

    def test_read_route_never_writes_proposal_content(self, client, vault):
        path = vault / "00 - Inbox" / "Skill-Cluster-Proposals.md"
        before = path.read_text(encoding="utf-8")
        client.get("/api/proposals", headers=_auth())
        assert path.read_text(encoding="utf-8") == before

    def test_action_routes_registered(self):
        paths = {route.path: route for route in web_routes()}
        assert "/api/proposals" in paths
        assert "/api/action/proposal" in paths
        assert "GET" in (paths["/api/proposals"].methods or set())
        assert "POST" in (paths["/api/action/proposal"].methods or set())
