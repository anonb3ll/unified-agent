"""Scout task shape (task #1027) — FirstMate-style investigation reports."""

from __future__ import annotations

from unified_agent.scout import (
    DecisionItem,
    apply_scout_finish,
    build_scout_report,
    default_report_relpath,
    evidence_ref_for_report,
    is_scout,
    render_decision_inventory,
    report_path_from_evidence,
    report_path_from_notes,
    report_path_from_text,
    scout_executor_prompt,
    validate_scout_report,
)


def test_is_scout_markers():
    assert is_scout("[task-shape: scout]\ninvestigate X")
    assert is_scout("prefix\n[scout]\n")
    assert not is_scout("[code-task] ship it")
    assert not is_scout("talk about `[scout]` in backticks")


def test_render_decision_inventory_numbered():
    md = render_decision_inventory(
        [
            DecisionItem(
                decision="Enable Funnel for Grok?",
                options=["yes", "no", "defer"],
                recommendation="defer",
            ),
            {"decision": "Pick delivery mode", "options": "direct-pr / local-only"},
        ]
    )
    assert "## Decision inventory" in md
    assert "1. **Decision:** Enable Funnel for Grok?" in md
    assert "Options: yes / no / defer" in md
    assert "Recommendation: defer" in md
    assert "2. **Decision:** Pick delivery mode" in md


def test_validate_scout_report_requires_inventory():
    bare = "# Scout\n\n## Bottom line\n\nok\n"
    assert validate_scout_report(bare) == ["missing '## Decision inventory' section"]

    empty_inv = build_scout_report(
        topic="t",
        bottom_line="bl",
        findings=["a"],
        decisions=[],
    )
    assert any("no actionable" in p for p in validate_scout_report(empty_inv))

    good = build_scout_report(
        topic="Funnel",
        bottom_line="Keep Funnel off.",
        findings=["Issuer must match Funnel host."],
        decisions=[
            {
                "decision": "Enable Funnel?",
                "options": ["yes", "no"],
                "recommendation": "no",
            }
        ],
        sources=["https://example.com"],
        task_id=1027,
    )
    assert validate_scout_report(good) == []
    assert "source: unified-agent-scout" in good
    assert "task_id: 1027" in good


def test_apply_scout_finish_stamps_no_pr_and_review():
    stamped = apply_scout_finish(
        "investigate login flake", "00 - Inbox/Scout - login 2026-09-16.md"
    )
    assert "[task-shape: scout]" in stamped
    assert "[no-code]" in stamped
    assert "[waiting-for: review]" in stamped
    assert "[scout: report=00 - Inbox/Scout - login 2026-09-16.md]" in stamped
    assert "[scout: no-pr]" in stamped
    assert "prefer-pr" not in stamped
    assert report_path_from_notes(stamped) == "00 - Inbox/Scout - login 2026-09-16.md"


def test_report_path_extraction_and_evidence():
    text = "done\n[scout: report=00 - Inbox/Scout - x.md]\n"
    assert report_path_from_text(text) == "00 - Inbox/Scout - x.md"
    assert report_path_from_text("vault:00 - Inbox/Scout - x.md") == "00 - Inbox/Scout - x.md"
    ref = evidence_ref_for_report("00 - Inbox/Scout - x.md")
    assert ref == "vault:00 - Inbox/Scout - x.md"
    assert report_path_from_evidence(ref) == "00 - Inbox/Scout - x.md"


def test_scout_executor_prompt_forbids_pr():
    prompt = scout_executor_prompt(task_id=42, task_spec="look into X", from_agent="Desktop_Agent5")
    assert "SCOUT" in prompt
    assert "Do NOT open a PR" in prompt
    assert "## Decision inventory" in prompt
    assert "[scout: report=" in prompt
    assert default_report_relpath("task-42").startswith("00 - Inbox/Scout -")


def test_scan_includes_scout_despite_no_code(monkeypatch):
    """Scout + [no-code] must still appear in the review scan with kind=scout."""
    from unified_agent import reviewer

    class _Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

        def keys(self):
            return dict.keys(self)

    scout_row = _Row(
        id=1027,
        title="Scout login",
        status="doing",
        priority="med",
        notes=(
            "[task-shape: scout]\n[no-code]\n"
            "[scout: report=00 - Inbox/Scout - login.md]\n"
            "[waiting-for: review]"
        ),
        evidence_ref="vault:00 - Inbox/Scout - login.md",
        claimed_by="Desktop_Agent4",
    )
    plain_no_code = _Row(
        id=1,
        title="Docs only",
        status="doing",
        priority="low",
        notes="[no-code]\n[waiting-for: review]",
        evidence_ref=None,
        claimed_by="Desktop_Agent4",
    )

    class _Conn:
        def execute(self, *_a, **_k):
            class _R:
                def fetchall(self_inner):
                    return [scout_row, plain_no_code]

            return _R()

        def close(self):
            return None

    monkeypatch.setattr(reviewer.storage, "require_live_task_store", lambda: None)
    monkeypatch.setattr(reviewer.storage, "connect", lambda: _Conn())
    monkeypatch.setattr(reviewer, "_is_open_boundary_review_spec", lambda *_a, **_k: False)
    monkeypatch.setattr(reviewer, "_is_active_review_handoff", lambda *_a, **_k: False)

    result = reviewer.scan_for_reviewable_tasks()
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["id"] == 1027
    assert result["tasks"][0]["kind"] == "scout"


def test_review_scout_task_ready(monkeypatch, tmp_path):
    from unified_agent import reviewer
    from unified_agent.scout import build_scout_report

    report = build_scout_report(
        topic="X",
        bottom_line="Y",
        findings=["z"],
        decisions=[{"decision": "Ship?", "options": ["yes", "no"], "recommendation": "no"}],
    )
    path = "00 - Inbox/Scout - X.md"
    notes = f"[task-shape: scout]\n[no-code]\n[scout: report={path}]\n\n[waiting-for: review]"
    appended: list[str] = []

    monkeypatch.setattr(
        reviewer,
        "_append_note",
        lambda _id, line: appended.append(line),
    )
    monkeypatch.setattr(
        "unified_agent.tools.vault.vault_read",
        lambda p, offset=0, limit=2000: {
            "path": p,
            "content": report,
            "total_lines": report.count("\n") + 1,
            "truncated": False,
        },
    )

    class _Conn:
        def execute(self, *_a, **_k):
            return self

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(reviewer.storage, "connect", lambda: _Conn())
    monkeypatch.setattr(reviewer.storage, "now", lambda: "2026-09-16T00:00:00+00:00")

    result = reviewer._review_scout_task(
        1027, "Scout X", notes, evidence_ref=f"vault:{path}", dry_run=False
    )
    assert result["verdict"] == "scout-ready"
    assert "Decision inventory" in (result["notification"] or "")
    assert any("scout-ready" in line for line in appended)
    assert "no PR" in (result["notification"] or "")
