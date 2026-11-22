"""Plan review ::preview widget (UA #838 / ECC-5).

The desktop already frames `::preview{file=…}` and routes `data-hermes-send`
through `window.hermes.send`. These tests lock the hub-side contract: escaped
HTML, a fixed intent schema, live rewrite of the same preview file, and
verdicts that reuse the canonical `[waiting-for: plan-approval]` /
`[plan: approved @ ts]` gate. No parallel approval store.
"""

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.reviewer import WAITING_FOR_PLAN_APPROVAL, review_plan_approvals
from unified_agent.tools.plan import plan_create, plan_get, plan_manage
from unified_agent.tools.tasks import task_add, task_update


def _gate_task(plan_id: str, status: str = "doing") -> int:
    notes = f"{WAITING_FOR_PLAN_APPROVAL}\nplan: {plan_id}"
    task_id = task_add("approve-my-plan", notes=notes)["id"]
    task_update(task_id, status=status, worktree=False)
    return task_id


def _task_notes(task_id: int) -> str:
    from unified_agent import storage

    conn = storage.connect()
    try:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()["notes"]
    finally:
        conn.close()


class TestPlanPreviewWidget:
    def test_preview_writes_escaped_numbered_sections_and_directive(self):
        pid = plan_create(
            'Ship <b>it</b> & "go"',
            ["design <img src=x onerror=alert(1)>", "build"],
        )["plan_id"]
        out = plan_manage("preview", {"plan_id": pid})
        assert out["directive"] == f'::preview{{file=".unified-agent/plan-preview/{pid}.html"}}'
        html_path = Path(out["html_path"])
        assert html_path.is_file()
        html = html_path.read_text(encoding="utf-8")
        assert "<ol>" in html
        assert "1." in html or "<li" in html
        assert "<b>it</b>" not in html
        assert "&lt;b&gt;it&lt;/b&gt;" in html
        assert "<img src=x" not in html
        assert "&lt;img src=x" in html
        assert f'data-hermes-send="plan-review v1 approve {pid}"' in html
        assert f'data-hermes-send="plan-review v1 changes {pid}"' in html
        assert "data-plan-annotate=" in html
        assert "<script>alert" not in html
        assert out["fingerprint"]

    def test_preview_rejects_script_tags_from_plan_body(self):
        pid = plan_create("<script>window.hack=1</script>", ["ok"])["plan_id"]
        html = Path(plan_manage("preview", {"plan_id": pid})["html_path"]).read_text(
            encoding="utf-8"
        )
        assert "<script>window.hack=1</script>" not in html
        assert "&lt;script&gt;window.hack=1&lt;/script&gt;" in html

    def test_preview_buttons_only_use_fixed_intent_schema(self):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        html = Path(plan_manage("preview", {"plan_id": pid})["html_path"]).read_text(
            encoding="utf-8"
        )
        sends = []
        needle = 'data-hermes-send="'
        start = 0
        while True:
            i = html.find(needle, start)
            if i < 0:
                break
            j = html.find('"', i + len(needle))
            sends.append(html[i + len(needle) : j])
            start = j + 1
        assert sends == [
            f"plan-review v1 approve {pid}",
            f"plan-review v1 changes {pid}",
        ]
        assert all(len(s) <= 500 for s in sends)

    def test_preview_rewrites_same_path_when_plan_changes(self):
        from unified_agent.tools.plan import plan_update

        pid = plan_create("g", ["a", "b"])["plan_id"]
        first = plan_manage("preview", {"plan_id": pid})
        plan_update(pid, 0, status="done")
        second = plan_manage("preview", {"plan_id": pid})
        assert first["html_path"] == second["html_path"]
        assert first["directive"] == second["directive"]
        assert first["fingerprint"] != second["fingerprint"]
        html = Path(second["html_path"]).read_text(encoding="utf-8")
        assert first["fingerprint"] not in html
        assert second["fingerprint"] in html


class TestPlanReviewVerdicts:
    def test_approve_replaces_waiting_marker_and_is_idempotent(self):
        pid = plan_create("g", ["a"])["plan_id"]
        tid = _gate_task(pid)
        first = plan_manage("review", {"intent": f"plan-review v1 approve {pid}", "task_id": tid})
        assert first["applied"] is True
        assert first["already"] is False
        assert first["action"] == "approve"
        notes = _task_notes(tid)
        assert WAITING_FOR_PLAN_APPROVAL not in notes
        assert "[plan: approved @" in notes
        assert f"plan: {pid}" in notes
        second = plan_manage("review", {"intent": f"plan-review v1 approve {pid}", "task_id": tid})
        assert second["already"] is True
        assert _task_notes(tid).count("[plan: approved @") == 1
        assert review_plan_approvals()["results"] == []
        assert plan_get(pid)["plan"]["status"] == "active"

    def test_approve_without_plan_approval_gate_is_refused(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="waiting-for: plan-approval"):
            plan_manage("review", {"intent": f"plan-review v1 approve {pid}"})
        notes_blob = " ".join(
            t.get("notes") or ""
            for t in __import__("unified_agent.tools.tasks", fromlist=["task_list"]).task_list(
                status="all"
            )["tasks"]
        )
        assert "[plan: approved @" not in notes_blob

    def test_request_changes_keeps_human_gate(self):
        pid = plan_create("g", ["a"])["plan_id"]
        tid = _gate_task(pid)
        first = plan_manage("review", {"intent": f"plan-review v1 changes {pid}", "task_id": tid})
        assert first["applied"] is True
        notes = _task_notes(tid)
        assert WAITING_FOR_PLAN_APPROVAL in notes
        assert "[plan: approved @" not in notes
        assert "[plan: changes-requested @" in notes
        second = plan_manage("review", {"intent": f"plan-review v1 changes {pid}", "task_id": tid})
        assert second["already"] is True
        assert _task_notes(tid).count("[plan: changes-requested @") == 1
        assert review_plan_approvals()["results"]

    def test_annotate_round_trips_escaped_and_idempotent(self):
        pid = plan_create("g", ["design", "build"])["plan_id"]
        tid = _gate_task(pid)
        comment = "<img src=x onerror=alert(1)> ship it"
        intent = f"plan-review v1 annotate {pid} 0 {comment}"
        first = plan_manage("review", {"intent": intent, "task_id": tid})
        assert first["applied"] is True
        step_note = plan_get(pid)["steps"][0]["note"]
        assert "ship it" in step_note
        assert "[plan-annotation:" in step_note
        html = Path(first["html_path"]).read_text(encoding="utf-8")
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img src=x" in html
        assert "ship it" in html
        second = plan_manage("review", {"intent": intent, "task_id": tid})
        assert second["already"] is True
        assert plan_get(pid)["steps"][0]["note"].count("[plan-annotation:") == 1

    def test_malformed_intent_is_rejected(self):
        pid = plan_create("g", ["a"])["plan_id"]
        with pytest.raises(ToolError, match="intent"):
            plan_manage("review", {"intent": "do something evil"})
        with pytest.raises(ToolError, match="intent"):
            plan_manage("review", {"intent": f"plan-review v1 approve {pid};drop table"})
        with pytest.raises(ToolError, match="intent"):
            plan_manage("review", {"intent": f"plan-review v1 annotate {pid} 0"})

    def test_research_can_preview_but_not_review(self, monkeypatch):
        from unified_agent import config, profiles

        pid = plan_create("g", ["a"])["plan_id"]
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        identity = config.agent_identity_var.set("Desktop_Agent2")
        session = config.mcp_session_id_var.set("plan-preview-research")
        try:
            profiles.activate("research")
            out = plan_manage("preview", {"plan_id": pid})
            assert out["directive"].startswith("::preview{file=")
            with pytest.raises(ToolError, match="not exposed"):
                plan_manage("review", {"intent": f"plan-review v1 approve {pid}"})
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.clear()
