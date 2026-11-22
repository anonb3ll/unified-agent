"""Per-agent delegation ledger monthly page (UA #703, Buzz-emulation #2).

Covers the ledger line format (agent, task id, verdict, CI state), month
rollover (page chosen at write time from the clock), and append-only
behavior (existing pages only appended, missing months bootstrapped, locked
pages never forced). The CLI wiring (best-effort, exit-code preserving) is
exercised here too via review_cli.main.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import delegation_ledger_log, review_cli, reviewer, storage
from unified_agent.delegation_ledger_log import (
    LEDGER_PAGE_FMT,
    append_review_ledger,
    ledger_line,
)
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.tools.vault import vault_write

WAITING = reviewer.WAITING_FOR_REVIEW


# ── fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Hermetic vault with the Logs write-dir allowlisted (task #544 shape)."""
    import unified_agent.config as config

    v = tmp_path / "vault"
    (v / "Logs").mkdir(parents=True)
    monkeypatch.setattr(config, "vault_root", lambda: v)
    monkeypatch.setattr(config, "vault_write_dirs", lambda: ["00 - Inbox", "Logs", "Wiki"])
    return v


def _candidate(agent: str | None = None) -> int:
    """A task the pipeline would review, optionally claimed by an agent."""
    t = task_add("ledger-me", notes=f"{WAITING} ready")
    if agent:
        task_claim(t["id"], agent)
    else:
        task_update(t["id"], status="doing")
    return t["id"]


def _result(task_id: int, verdict: str, notification: str | None = "state changed") -> dict:
    return {
        "task_id": task_id,
        "verdict": verdict,
        "red_tier_files": [],
        "merge_result": None,
        "notification": notification,
    }


def _read(vault, page: str) -> str:
    return (vault / page).read_text()


# ── line format ──────────────────────────────────────────────────────


class TestLedgerLine:
    def test_line_format_with_structured_claim(self, isolated_env, vault):
        tid = _candidate("Desktop_agent3")
        line = ledger_line(tid, "validated")
        parts = line.split(" | ")
        assert parts[0].startswith("- 20") and parts[0].endswith("Z")
        assert parts[1] == "Desktop_agent3"
        assert parts[2] == f"#{tid}"
        assert parts[3] == "validated"
        assert parts[4] == "ci:green"

    def test_agent_falls_back_to_notes_marker(self, isolated_env, vault):
        tid = _candidate()
        conn = storage.connect()
        try:
            notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
        finally:
            conn.close()
        task_update(tid, notes=f"{notes}\nclaimed-by: Laptop_Agent2")
        assert " | Laptop_Agent2 | " in ledger_line(tid, "merged")

    def test_agent_unknown_when_never_claimed(self, isolated_env, vault):
        tid = task_add("unclaimed")["id"]
        assert " | unknown | " in ledger_line(tid, "failed")

    def test_ci_states(self, isolated_env, vault):
        tid = task_add("ci-map")["id"]
        assert ledger_line(tid, "red-tier").endswith("ci:red-tier")
        assert ledger_line(tid, "failed").endswith("ci:red")
        assert ledger_line(tid, "validated").endswith("ci:green")
        assert ledger_line(tid, "unresolvable").endswith("ci:n/a")

    def test_rejects_missing_task_id_and_verdict(self, isolated_env, vault):
        with pytest.raises(ValueError):
            ledger_line(0, "validated")
        with pytest.raises(ValueError):
            ledger_line(1, "  ")

    def test_pinned_now_stamps_the_line(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = task_add("pinned")["id"]
        now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
        assert ledger_line(tid, "validated", now=now).startswith("- 2026-10-01T12:00:00Z |")


# ── month page + rollover ────────────────────────────────────────────


class TestMonthRollover:
    def test_creates_missing_month_with_minimal_header(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_Agent1")
        now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
        out = append_review_ledger([_result(tid, "validated")], now=now)
        assert out == {
            "page": "Logs/Delegation Ledger 2026-09.md",
            "appended": 1,
            "created": True,
        }
        text = _read(vault, out["page"])
        assert text.startswith("# Delegation Ledger — 2026-09\n")
        assert f"| Desktop_Agent1 | #{tid} | validated | ci:green" in text

    def test_month_rolls_automatically(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_agent3")
        sep = datetime(2026, 9, 30, 23, 0, 0, tzinfo=UTC)
        oct1 = datetime(2026, 10, 1, 0, 30, 0, tzinfo=UTC)
        append_review_ledger([_result(tid, "validated")], now=sep)
        out = append_review_ledger([_result(tid, "merged")], now=oct1)

        # A new page was created for October…
        assert out["page"] == LEDGER_PAGE_FMT.format(ym="2026-10")
        assert out["created"] is True
        # …and September was never rewritten — its single line is untouched.
        sep_text = _read(vault, "Logs/Delegation Ledger 2026-09.md")
        assert sep_text.count(" | Desktop_agent3 | ") == 1
        assert "validated" in sep_text and "merged" not in sep_text

    def test_repeated_appends_land_on_one_page(self, isolated_env, vault):
        from datetime import UTC, datetime

        now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
        t1, t2 = _candidate("Desktop_Agent1"), _candidate("Desktop_agent3")
        append_review_ledger([_result(t1, "validated")], now=now)
        out = append_review_ledger([_result(t2, "failed")], now=now)
        assert out["created"] is False  # existing page, appended only
        text = _read(vault, out["page"])
        assert f"#{t1} | validated" in text
        assert f"#{t2} | failed | ci:red" in text


# ── append-only behavior ─────────────────────────────────────────────


class TestAppendOnly:
    def test_existing_lines_never_rewritten(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_Agent1")
        sep = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
        oct1 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
        page_sep = LEDGER_PAGE_FMT.format(ym="2026-09")
        page_oct = LEDGER_PAGE_FMT.format(ym="2026-10")

        append_review_ledger([_result(tid, "validated")], now=sep)
        sep_before = _read(vault, page_sep)
        append_review_ledger([_result(tid, "merged")], now=oct1)

        # September's bytes are byte-identical after the October write.
        assert _read(vault, page_sep) == sep_before
        oct_text = _read(vault, page_oct)
        assert f"#{tid} | merged | ci:green" in oct_text

    def test_locked_page_is_never_forced(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_Agent1")
        page2 = "Logs/Delegation Ledger 2026-08.md"
        vault_write(page2, "---\nlocked: true\n---\n\n# locked page\n", force=True)
        now = datetime(2026, 8, 15, 8, 0, 0, tzinfo=UTC)
        # The refusal propagates (the CLI's best-effort wrapper catches it) —
        # the ledger never forces a locked page open.
        with pytest.raises(ToolError, match="locked"):
            append_review_ledger([_result(tid, "failed")], now=now)
        assert _read(vault, page2) == "---\nlocked: true\n---\n\n# locked page\n"

    def test_lines_only_for_state_changes(self, isolated_env, vault):
        """Unchanged polls (notification=None) must not flood the ledger."""
        from datetime import UTC, datetime

        tid = _candidate("Desktop_agent3")
        now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
        out = append_review_ledger(
            [
                _result(tid, "validated", notification="held for human merge"),
                _result(tid, "unchanged", notification=None),
                _result(tid, "unchanged", notification=None),
            ],
            now=now,
        )
        assert out["appended"] == 1
        text = _read(vault, out["page"])
        assert "validated" in text
        assert "unchanged" not in text

    def test_no_results_no_page(self, isolated_env, vault):
        out = append_review_ledger([])
        assert out == {"page": None, "appended": 0, "created": False}
        assert not list((vault / "Logs").glob("Delegation Ledger*"))

    def test_unlocked_page_content_preserved_then_appended(self, isolated_env, vault):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_Agent2")
        page = LEDGER_PAGE_FMT.format(ym="2026-09")
        vault_write(page, "# Delegation Ledger — 2026-09\n\nhandwritten preamble\n")
        before = _read(vault, page)
        now = datetime(2026, 9, 16, 8, 0, 0, tzinfo=UTC)
        append_review_ledger([_result(tid, "red-tier")], now=now)
        after = _read(vault, page)
        assert after.startswith(before)  # preamble untouched, line appended after
        assert "ci:red-tier" in after[len(before) :]


# ── CLI wiring (best-effort, exit-code preserving) ───────────────────


class TestCliWiring:
    def test_scan_writes_ledger_and_keeps_exit_code(self, isolated_env, vault, monkeypatch, capsys):
        from datetime import UTC, datetime

        tid = _candidate("Desktop_Agent1")
        now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(delegation_ledger_log, "_now", lambda: now)
        monkeypatch.setattr(
            reviewer,
            "reconcile_tasks",
            lambda send_notifications=False, dry_run=False: {"results": []},
        )
        monkeypatch.setattr(
            reviewer,
            "review_plan_approvals",
            lambda send_notifications=False, dry_run=False: {"results": []},
        )
        monkeypatch.setattr(
            reviewer,
            "review_and_notify",
            lambda task_id, dry_run=False: _result(task_id, "validated"),
        )
        monkeypatch.setattr(reviewer, "scan_for_reviewable_tasks", lambda: {"tasks": [{"id": tid}]})

        code = review_cli.main(["scan"])
        out = capsys.readouterr()
        assert code == 0
        assert "ledger: 1 line(s)" in out.out
        page = LEDGER_PAGE_FMT.format(ym="2026-09")
        text = _read(vault, page)
        assert f"| Desktop_Agent1 | #{tid} | validated | ci:green" in text

    def test_ledger_failure_is_warning_not_exit_change(
        self, isolated_env, vault, monkeypatch, capsys
    ):
        tid = _candidate("Desktop_agent3")
        monkeypatch.setattr(
            reviewer,
            "reconcile_tasks",
            lambda send_notifications=False, dry_run=False: {"results": []},
        )
        monkeypatch.setattr(
            reviewer,
            "review_plan_approvals",
            lambda send_notifications=False, dry_run=False: {"results": []},
        )
        monkeypatch.setattr(
            reviewer,
            "review_and_notify",
            lambda task_id, dry_run=False: _result(task_id, "failed", "❌ boom"),
        )
        monkeypatch.setattr(reviewer, "scan_for_reviewable_tasks", lambda: {"tasks": [{"id": tid}]})
        # Vault unavailable entirely — the worst ledger failure there is.
        import unified_agent.config as config

        monkeypatch.setattr(config, "vault_root", lambda: None)

        code = review_cli.main(["scan", "--no-notify"])
        out, err = capsys.readouterr()
        assert code == 1  # the failed verdict still drives the exit code
        assert "delegation ledger append failed" in err
