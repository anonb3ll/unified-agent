import contextlib
import contextvars
import datetime
import hashlib
import os
import re
import stat
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, note_lock, profiles, storage
from unified_agent.safety import resolve_in_vault
from unified_agent.tools.vault import (
    _count_note_lines,
    note_write_lock,
    note_write_locks,
    vault_archive_plan,
    vault_briefing_digest,
    vault_ingest_status,
    vault_journal,
    vault_links,
    vault_list,
    vault_move,
    vault_read,
    vault_search,
    vault_write,
)


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("Laptop_Agent5")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A PARA-style vault matching ~/vaults/main, isolated under tmp_path."""
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "01 - Projects", "Logs", ".obsidian"):
        (root / d).mkdir(parents=True)
    (root / "01 - Projects" / "hermes.md").write_text(
        "# Hermes\n\nSupervisor persona. See [[unified-agent]] and [[COLLABORATION|protocol]].\n"
    )
    (root / "01 - Projects" / "unified-agent.md").write_text("# unified-agent\n\nTool server.\n")
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    return root


@pytest.fixture
def cos_session(monkeypatch):
    """An authenticated session with the chief-of-staff profile activated, for vault_move."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Laptop_Agent5")
    session = config.mcp_session_id_var.set("mcp-session-move")
    with profiles._lock:
        profiles._active.clear()
    profiles.activate("chief-of-staff")
    try:
        yield
    finally:
        with profiles._lock:
            profiles._active.clear()
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


class TestVaultSandbox:
    def test_read_anywhere_in_vault(self, vault):
        assert vault_read("01 - Projects/hermes.md")["total_lines"] == 3

    def test_dotdot_escape_rejected(self, vault):
        with pytest.raises(ToolError, match="escapes the vault"):
            resolve_in_vault("../outside.md")

    def test_absolute_outside_rejected(self, vault):
        with pytest.raises(ToolError, match="escapes the vault"):
            resolve_in_vault("/etc/passwd")

    def test_symlink_escape_rejected(self, vault, tmp_path):
        outside = tmp_path / "secret.md"
        outside.write_text("secret")
        os.symlink(outside, vault / "00 - Inbox" / "link.md")
        with pytest.raises(ToolError, match="escapes the vault"):
            resolve_in_vault("00 - Inbox/link.md")

    def test_hidden_dirs_blocked(self, vault):
        with pytest.raises(ToolError, match="hidden"):
            resolve_in_vault(".obsidian/app.json")

    def test_disabled_when_env_empty(self, vault, monkeypatch):
        monkeypatch.setenv("UA_VAULT_PATH", "")
        with pytest.raises(ToolError, match="disabled"):
            resolve_in_vault("anything.md")

    def test_missing_root_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_VAULT_PATH", str(tmp_path / "nope"))
        with pytest.raises(ToolError, match="does not exist"):
            resolve_in_vault("anything.md")


class TestVaultWrites:
    def test_write_to_inbox_allowed(self, vault):
        out = vault_write("00 - Inbox/capture.md", "# Capture\n")
        assert (vault / "00 - Inbox" / "capture.md").read_text() == "# Capture\n"
        assert out["path"] == "00 - Inbox/capture.md"

    def test_write_to_wiki_allowed_by_default(self, vault):
        out = vault_write("Wiki/Index.md", "# Wiki index\n")
        assert (vault / "Wiki" / "Index.md").read_text() == "# Wiki index\n"
        assert out["path"] == "Wiki/Index.md"

    def test_write_to_curated_dir_rejected(self, vault):
        with pytest.raises(ToolError, match="restricted to"):
            vault_write("01 - Projects/hermes.md", "clobbered")

    def test_write_to_root_rejected(self, vault):
        with pytest.raises(ToolError, match="restricted to"):
            vault_write("stray.md", "x")

    def test_non_markdown_write_rejected(self, vault):
        with pytest.raises(ToolError, match=r"limited to .*\.txt"):
            vault_write("00 - Inbox/evil.sh", "rm -rf /")

    def test_write_dirs_env_extends_allowlist(self, vault, monkeypatch):
        monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "00 - Inbox:Logs:01 - Projects")
        vault_write("01 - Projects/new-note.md", "ok")
        assert (vault / "01 - Projects" / "new-note.md").exists()

    def test_append(self, vault):
        vault_write("00 - Inbox/a.md", "one\n")
        vault_write("00 - Inbox/a.md", "two\n", append=True)
        assert (vault / "00 - Inbox" / "a.md").read_text() == "one\ntwo\n"

    def test_mode_append(self, vault):
        vault_write("00 - Inbox/m.md", "one\n")
        out = vault_write("00 - Inbox/m.md", "two\n", mode="append")
        assert (vault / "00 - Inbox" / "m.md").read_text() == "one\ntwo\n"
        assert out["path"] == "00 - Inbox/m.md"

    def test_mode_overwrite_is_default(self, vault):
        vault_write("00 - Inbox/o.md", "first\n")
        vault_write("00 - Inbox/o.md", "second\n", mode="overwrite")
        assert (vault / "00 - Inbox" / "o.md").read_text() == "second\n"

    def test_unknown_mode_rejected_not_silently_overwritten(self, vault):
        # Regression for task #854: mode="apend" (typo) or any non-mode keyword
        # must error, not fall through to a file-clobbering overwrite.
        vault_write("00 - Inbox/keep.md", "important\n")
        with pytest.raises(ToolError, match="mode must be one of"):
            vault_write("00 - Inbox/keep.md", "stub", mode="apend")
        assert (vault / "00 - Inbox" / "keep.md").read_text() == "important\n"

    def test_append_true_and_mode_append_agree(self, vault):
        vault_write("00 - Inbox/both.md", "a\n")
        vault_write("00 - Inbox/both.md", "b\n", append=True, mode="append")
        assert (vault / "00 - Inbox" / "both.md").read_text() == "a\nb\n"

    def test_result_echoes_effective_mode(self, vault):
        # Regression for task #1165: a client with a stale schema snapshot can
        # silently drop `mode` (the kwarg never reaches the server, so the
        # unknown-kwarg guard cannot fire) and an intended append becomes a
        # clobbering overwrite. The write result must echo the EFFECTIVE mode
        # and post-write shape so intent-vs-result mismatches are visible at
        # write time.
        out_overwrite = vault_write("00 - Inbox/echo.md", "one\ntwo\n")
        assert out_overwrite["mode"] == "overwrite"
        assert out_overwrite["append"] is False
        assert out_overwrite["total_lines_after"] == 2
        out_append = vault_write("00 - Inbox/echo.md", "three\n", mode="append")
        assert out_append["mode"] == "append"
        assert out_append["append"] is True
        assert out_append["total_lines_after"] == 3
        assert (vault / "00 - Inbox" / "echo.md").read_text() == "one\ntwo\nthree\n"

    def test_legacy_append_true_echoes_append_mode(self, vault):
        # Devin review of PR #617: the legacy `append=True` spelling appends
        # but echoed the default `mode="overwrite"` — clients checking the
        # effective mode were told the opposite operation happened.
        out = vault_write("00 - Inbox/legacy.md", "one\n")
        assert out["mode"] == "overwrite"
        out = vault_write("00 - Inbox/legacy.md", "two\n", append=True)
        assert out["mode"] == "append"
        assert out["append"] is True
        assert out["total_lines_after"] == 2
        assert (vault / "00 - Inbox" / "legacy.md").read_text() == "one\ntwo\n"

    def test_total_lines_after_matches_splitlines_semantics(self, vault):
        # Devin review of PR #621: str.splitlines() splits on more boundaries
        # than file-object iteration (universal newlines only), so the echo
        # must agree with vault_read for \u2028-class separators too.
        (vault / "00 - Inbox" / "uni.md").write_text("first\u2028second\n", encoding="utf-8")
        out = vault_write("00 - Inbox/uni.md", "tail", append=True)
        # splitlines("first\u2028second\ntail") == 3 — file iteration (universal
        # newlines only) would report 2, disagreeing with vault_read.
        assert out["total_lines_after"] == 3

    def test_line_counter_honors_chunk_boundaries(self, vault):
        # The streamed counter must not double-count a CRLF pair, split a
        # multi-byte separator, or lose a final unterminated line when the
        # data happens to cut mid-boundary — force tiny chunks to prove it.
        note = vault / "00 - Inbox" / "chunks.md"
        for raw, want in [
            (b"a\r\nb\r\nc", 3),  # CRLF pairs at chunk edges
            (b"a\rb", 2),  # lone CR is a boundary
            (b"x\xe2\x80" + b"\xa8y", 2),  # U+2028 split mid-UTF-8
            (b"a\r", 1),  # trailing CR at EOF
            (b"a\n\nb", 3),  # empty line between boundaries
            (b"solo", 1),  # no boundary at all
        ]:
            note.write_bytes(raw)
            assert _count_note_lines(note, chunk_size=2) == want, raw

    def test_giant_single_line_stays_bounded(self, vault):
        # Devin review of PR #621 (🔴): readline()-style iteration buffers a
        # single multi-GB line wholesale. The counter must hold only one
        # chunk. A 4 MB single-line note exercises the path (4 GB would be
        # too slow for the suite; the algorithm is size-independent).
        note = vault / "00 - Inbox" / "giant.md"
        note.write_text("z" * (4 * 1024 * 1024) + "\n", encoding="utf-8")
        out = vault_write("00 - Inbox/giant.md", "tail\n", append=True)
        assert out["total_lines_after"] == 2


class TestVaultReadListSearch:
    def test_list_hides_dot_dirs(self, vault):
        names = [e["name"] for e in vault_list(".")["entries"]]
        assert ".obsidian" not in names
        assert "01 - Projects" in names

    def test_search_finds_note(self, vault):
        matches = vault_search("Supervisor persona")["matches"]
        assert matches and matches[0]["path"] == "01 - Projects/hermes.md"

    def test_search_skips_hidden(self, vault):
        (vault / ".obsidian" / "workspace.json").write_text("Supervisor persona")
        assert all(
            not m["path"].startswith(".obsidian")
            for m in vault_search("Supervisor persona")["matches"]
        )


class TestVaultLinks:
    def test_outgoing_links(self, vault):
        out = vault_links("01 - Projects/hermes.md")
        assert out["outgoing"] == ["COLLABORATION", "unified-agent"]

    def test_backlinks(self, vault):
        out = vault_links("01 - Projects/unified-agent.md")
        assert out["backlinks"] == ["01 - Projects/hermes.md"]


class TestVaultJournal:
    def test_unreadable_note_reports_actionable_error_without_oserror_text(
        self, vault, monkeypatch
    ):
        import builtins
        from pathlib import Path

        from unified_agent.tools import vault as vault_tools

        day = "2026-10-14"
        note = vault / "Logs" / f"{day}.md"
        note.write_text("existing journal\n")
        real_open = builtins.open

        def unreadable(file, *args, **kwargs):
            if isinstance(file, (str, Path)) and Path(file) == note:
                raise PermissionError("sensitive backend details")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(vault_tools, "open", unreadable, raising=False)
        with pytest.raises(ToolError, match="journal.*permission|journal.*PermissionError") as exc:
            vault_journal("append", agent="Desktop_Agent2", date=day)
        assert "sensitive backend details" not in str(exc.value)
        monkeypatch.undo()
        assert note.read_text() == "existing journal\n"

    def test_creates_daily_note_with_frontmatter(self, vault):
        out = vault_journal("Implemented vault tools", agent="Desktop_Agent1")
        assert out["created_note"] is True
        text = (vault / out["path"]).read_text()
        assert text.startswith("---\ndate: ")
        assert "[Desktop_Agent1] Implemented vault tools" in text

    def test_appends_and_tags(self, vault):
        vault_journal("first", agent="Desktop_Agent1")
        out = vault_journal("second entry", agent="Desktop_agent3", tags=["decision", "#hermes"])
        assert out["created_note"] is False
        text = (vault / out["path"]).read_text()
        assert "[Desktop_Agent1] first" in text
        assert "[Desktop_agent3] second entry #decision #hermes" in text

    def test_newlines_collapsed(self, vault):
        out = vault_journal("line one\nline two", agent="Desktop_Agent1")
        assert "line one line two" in out["entry"]

    def test_empty_entry_rejected(self, vault):
        with pytest.raises(ToolError, match="empty"):
            vault_journal("   \n ", agent="Desktop_Agent1")

    def test_agent_label_required(self, vault):
        with pytest.raises(ToolError, match="agent label"):
            vault_journal("something", agent="  ")

    def test_explicit_date(self, vault):
        # Explicit date creates a new file if not present
        out = vault_journal("historic event", agent="Desktop_agent3", date="2026-07-15")
        assert out["created_note"] is True
        assert out["path"] == "Logs/2026-07-15.md"
        text = (vault / out["path"]).read_text()
        assert "date: 2026-07-15" in text
        assert "[Desktop_agent3] historic event" in text

        # Appending to the same explicit date
        out2 = vault_journal("another event", agent="Desktop_Agent1", date="2026-07-15")
        assert out2["created_note"] is False
        assert out2["path"] == "Logs/2026-07-15.md"
        text2 = (vault / out2["path"]).read_text()
        assert "[Desktop_agent3] historic event" in text2
        assert "[Desktop_Agent1] another event" in text2

    def test_invalid_date_format(self, vault):
        with pytest.raises(ToolError, match="date must be in YYYY-MM-DD format"):
            vault_journal("bad date format", agent="Desktop_agent3", date="15-07-2026")

        with pytest.raises(ToolError, match="date must be in YYYY-MM-DD format"):
            vault_journal("bad date format", agent="Desktop_agent3", date="2026/07/15")

    def test_invalid_calendar_date(self, vault):
        with pytest.raises(ToolError, match="date must be a valid calendar date"):
            vault_journal("invalid date", agent="Desktop_agent3", date="2026-02-30")

        with pytest.raises(ToolError, match="date must be a valid calendar date"):
            vault_journal("invalid date", agent="Desktop_agent3", date="2026-13-01")

    def test_oversized_entry_rejected(self, vault):
        """Task #379: entries over the cap are rejected, not truncated — a
        truncation could silently drop an observation with no other home."""
        from unified_agent.tools.vault import _JOURNAL_ENTRY_MAX_CHARS

        long_entry = "x" * (_JOURNAL_ENTRY_MAX_CHARS + 1)
        with pytest.raises(ToolError, match="bookkeeping index"):
            vault_journal(long_entry, agent="Desktop_Agent1")
        # Nothing was written — the daily note doesn't even exist.
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        assert not (vault / "Logs" / f"{today}.md").exists()

    def test_entry_at_cap_boundary_accepted(self, vault):
        from unified_agent.tools.vault import _JOURNAL_ENTRY_MAX_CHARS

        entry = "x" * _JOURNAL_ENTRY_MAX_CHARS
        out = vault_journal(entry, agent="Desktop_Agent1")
        assert entry in out["entry"]


def _write_journal(vault, rel_path: str, day: str, lines: list[str]) -> None:
    """Write a raw journal note (bypasses vault_journal's desktop-only path,
    so tests can populate Logs/Laptop/*.md the way a mirrored laptop would)."""
    text = f"---\ndate: {day}\ntype: journal\n---\n\n# {day} — Agent journal\n" + "".join(
        f"{line}\n" for line in lines
    )
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class TestJournalStream:
    """Task #1920: a job's per-event lines go to Logs/<stream>/<day>.md."""

    def test_appends_journal_format_lines_with_frontmatter(self, vault):
        from unified_agent.tools.vault import journal_stream_append

        out = journal_stream_append(
            "pr-watch", "PR #1 opened in o/r", agent="Desktop_Agent5", day="2026-10-07"
        )
        assert out["path"] == "Logs/pr-watch/2026-10-07.md" and out["created_note"]
        again = journal_stream_append(
            "pr-watch", "PR #2 merged", agent="Desktop_Agent5", day="2026-10-07", tags=["x"]
        )
        assert not again["created_note"]
        text = (vault / "Logs" / "pr-watch" / "2026-10-07.md").read_text()
        assert text.startswith("---\ndate: 2026-10-07\ntype: journal\nstream: pr-watch\n---\n")
        lines = [ln for ln in text.splitlines() if ln.startswith("- **")]
        assert len(lines) == 2 and lines[1].endswith("[Desktop_Agent5] PR #2 merged #x")
        # The shared daily journal is untouched.
        assert not (vault / "Logs" / "2026-10-07.md").exists()

    def test_rejects_bad_names_dates_and_long_entries(self, vault):
        from unified_agent.tools.vault import journal_stream_append

        for stream in ("../Wiki", "Pr Watch", "", "a/b"):
            with pytest.raises(ToolError, match="stream name"):
                journal_stream_append(stream, "x", agent="Desktop_Agent5", day="2026-10-07")
        with pytest.raises(ToolError, match="YYYY-MM-DD"):
            journal_stream_append("pr-watch", "x", agent="Desktop_Agent5", day="../x")
        with pytest.raises(ToolError, match="cap"):
            journal_stream_append("pr-watch", "x" * 241, agent="Desktop_Agent5", day="2026-10-07")

    def test_respects_lock_and_write_allowlist(self, vault, monkeypatch):
        from unified_agent.tools.vault import VaultLockedError, journal_stream_append

        note = vault / "Logs" / "pr-watch" / "2026-10-07.md"
        note.parent.mkdir(parents=True)
        note.write_text("---\nlocked: true\n---\n")
        with pytest.raises(VaultLockedError):
            journal_stream_append("pr-watch", "x", agent="Desktop_Agent5", day="2026-10-07")
        monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Wiki")
        with pytest.raises(ToolError, match="restricted"):
            journal_stream_append("pr-watch", "x", agent="Desktop_Agent5", day="2026-10-08")

    def test_journal_mentions(self, vault):
        from unified_agent.tools.vault import journal_mentions

        assert not journal_mentions("2026-07-15", "[[Logs/pr-watch/2026-07-15]]")
        vault_journal("see [[Logs/pr-watch/2026-07-15]]", agent="Desktop_Agent5", date="2026-07-15")
        assert journal_mentions("2026-07-15", "[[Logs/pr-watch/2026-07-15]]")

    def test_stream_records_activity(self, vault, monkeypatch):
        from unified_agent.tools.vault import journal_stream_append

        recorded = []
        monkeypatch.setattr(
            storage, "record_agent_event", lambda *a, **kw: recorded.append((a, kw))
        )
        journal_stream_append("pr-watch", "PR #123 merged", "Desktop_Agent5", "2026-10-07")
        assert recorded == [
            (
                ("journal",),
                {
                    "agent": "Desktop_Agent5",
                    "summary": "PR #123 merged",
                    "ref_id": "Logs/pr-watch/2026-10-07.md",
                },
            )
        ]

    def test_concurrent_pointer_has_one_line_and_activity(self, vault, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor

        from unified_agent.tools.vault import journal_pointer_append

        recorded = []
        monkeypatch.setattr(storage, "record_agent_event", lambda *a, **kw: recorded.append(kw))
        needle = "[[Logs/pr-watch/2026-10-07]]"

        def append(_):
            return journal_pointer_append(f"see {needle}", "Desktop_Agent5", "2026-10-07", needle)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(32)))
        assert (vault / "Logs/2026-10-07.md").read_text().count(needle) == 1
        assert len(recorded) == 1

    def test_briefing_includes_stream_events(self, vault):
        from unified_agent.tools.vault import journal_stream_append

        vault_journal("see [[Logs/pr-watch/2026-10-07]]", "Desktop_Agent5", "2026-10-07")
        journal_stream_append("pr-watch", "PR #321 merged", "Desktop_Agent5", "2026-10-07")
        entries = vault_briefing_digest(date="2026-10-07", include_inbox=False)["entries"]
        assert any(e["text"] == "PR #321 merged" and e["machine"] == "pr-watch" for e in entries)

    def test_pointer_cannot_append_to_locked_daily_note(self, vault):
        from unified_agent.tools.vault import VaultLockedError, journal_pointer_append

        note = vault / "Logs/2026-10-07.md"
        original = "---\nlocked: true\n---\n"
        note.write_text(original)
        needle = "[[Logs/pr-watch/2026-10-07]]"
        with pytest.raises(VaultLockedError):
            journal_pointer_append(f"see {needle}", "Desktop_Agent5", "2026-10-07", needle)
        assert note.read_text() == original

    def test_briefing_refuses_oversized_stream(self, vault):
        stream = vault / "Logs/pr-watch/2026-10-07.md"
        stream.parent.mkdir()
        stream.write_text("x" * (64 * 1024 + 1))
        with pytest.raises(ToolError, match="briefing limit"):
            vault_briefing_digest(date="2026-10-07", include_inbox=False)


class TestVaultBriefingDigest:
    def test_merges_desktop_and_laptop_chronologically(self, vault):
        _write_journal(
            vault,
            "Logs/2026-07-20.md",
            "2026-07-20",
            [
                "- **09:00** [Desktop_Agent1] first desktop thing",
                "- **11:00** [Desktop_Agent1] second desktop thing",
            ],
        )
        _write_journal(
            vault,
            "Logs/Laptop/2026-07-20.md",
            "2026-07-20",
            ["- **10:00** [Laptop_Agent1] laptop mid-morning note"],
        )
        result = vault_briefing_digest(date="2026-07-20")
        assert result["desktop_count"] == 2
        assert result["laptop_count"] == 1
        assert [e["time"] for e in result["entries"]] == ["09:00", "10:00", "11:00"]
        assert [e["machine"] for e in result["entries"]] == ["desktop", "laptop", "desktop"]

    def test_dedups_by_pr_url_keeps_first_seen(self, vault):
        _write_journal(
            vault,
            "Logs/2026-07-20.md",
            "2026-07-20",
            [
                "- **09:00** [Desktop_Agent1] Merged PR "
                "https://github.com/anonb3ll/unified-agent/pull/66. #ci"
            ],
        )
        _write_journal(
            vault,
            "Logs/Laptop/2026-07-20.md",
            "2026-07-20",
            [
                "- **09:05** [Laptop_Agent1] Merged PR "
                "https://github.com/anonb3ll/unified-agent/pull/66. #ci"
            ],
        )
        result = vault_briefing_digest(date="2026-07-20")
        assert result["deduped_count"] == 1
        assert len(result["entries"]) == 1
        assert result["entries"][0]["machine"] == "desktop"

    def test_dedups_by_bare_task_id(self, vault):
        _write_journal(
            vault,
            "Logs/2026-07-20.md",
            "2026-07-20",
            ["- **09:00** [Desktop_Agent1] Task #200 resolved."],
        )
        _write_journal(
            vault,
            "Logs/Laptop/2026-07-20.md",
            "2026-07-20",
            ["- **09:05** [Laptop_Agent1] Task #200 resolved (synced note)."],
        )
        result = vault_briefing_digest(date="2026-07-20")
        assert result["deduped_count"] == 1
        assert len(result["entries"]) == 1

    def test_no_dedup_without_a_reference(self, vault):
        _write_journal(
            vault, "Logs/2026-07-20.md", "2026-07-20", ["- **09:00** [Desktop_Agent1] status ok"]
        )
        _write_journal(
            vault,
            "Logs/Laptop/2026-07-20.md",
            "2026-07-20",
            ["- **09:05** [Laptop_Agent1] status ok"],
        )
        result = vault_briefing_digest(date="2026-07-20")
        assert result["deduped_count"] == 0
        assert len(result["entries"]) == 2

    def test_missing_journals_return_empty(self, vault):
        # include_inbox=False pins the journal-merge payload exactly; the inbox
        # section has its own coverage in TestBriefingDigestInboxTriage, and
        # delivery_slo/delivery_slo_line (task #1259) have their own coverage
        # in TestBriefingDigestDeliverySlo.
        result = vault_briefing_digest(date="2026-07-20", include_inbox=False)
        core = {k: v for k, v in result.items() if k not in ("delivery_slo", "delivery_slo_line")}
        assert core == {
            "date": "2026-07-20",
            "entries": [],
            "desktop_count": 0,
            "laptop_count": 0,
            "pr_watch_count": 0,
            "deduped_count": 0,
        }

    def test_invalid_date_format(self, vault):
        with pytest.raises(ToolError, match="date must be in YYYY-MM-DD format"):
            vault_briefing_digest(date="20-07-2026")

    def test_invalid_calendar_date(self, vault):
        with pytest.raises(ToolError, match="date must be a valid calendar date"):
            vault_briefing_digest(date="2026-02-30")

    def test_no_laptop_staleness_fields(self, vault):
        # The Logs/Laptop/ staleness trio (laptop_stale_hours / stale /
        # escalate) was removed with the dead namespace — task #761. An
        # ancient laptop file must not resurrect an escalation.
        _write_journal(
            vault,
            "Logs/Laptop/2020-01-01.md",
            "2020-01-01",
            ["- **00:00** [Laptop_Agent1] ancient note"],
        )
        result = vault_briefing_digest(include_inbox=False)
        assert "laptop_stale_hours" not in result
        assert "stale" not in result
        assert "escalate" not in result


class TestBriefingDigestDeliverySlo:
    """delivery_slo / delivery_slo_line (task #1259, Epic C #1188): the
    briefing's weekly SLO summary is not scoped to `date` — it always
    reflects the trailing 7 days as of now (see vault_briefing_digest's
    docstring for why)."""

    def test_empty_db_reports_uniformly_unavailable_line(self, vault):
        result = vault_briefing_digest(include_inbox=False)
        assert result["delivery_slo"] is not None
        assert result["delivery_slo"]["window_days"] == 7.0
        for key in (
            "dispatch_success_rate",
            "p90_cycle_time_hours",
            "unattributed_completions",
            "ci_green_rate",
        ):
            assert result["delivery_slo"][key]["status"] == "unavailable"
        assert result["delivery_slo_line"] == (
            "Delivery SLO (7d): dispatch n/a · p90 n/a · unattributed n/a · CI green n/a"
        )

    def test_line_reflects_real_dispatch_and_task_rows(self, vault):
        import uuid
        from datetime import UTC, datetime, timedelta

        one_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        conn = storage.connect()
        try:
            cur = conn.execute(
                "INSERT INTO tasks "
                "(title, status, priority, created_at, updated_at, completed_at, "
                "claimed_by, claimed_at) VALUES "
                "('slo task', 'done', 'med', ?, ?, ?, 'Desktop_Agent1', ?)",
                (one_day_ago, one_day_ago, one_day_ago, one_day_ago),
            )
            task_id = cur.lastrowid
            for _ in range(9):
                conn.execute(
                    "INSERT INTO dispatch_runs "
                    "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
                    "VALUES (?, ?, 'Desktop_Agent1', 'finished', 'Desktop_Agent1', ?, ?)",
                    (f"run-{uuid.uuid4().hex[:12]}", task_id, one_day_ago, one_day_ago),
                )
            conn.execute(
                "INSERT INTO dispatch_runs "
                "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES (?, ?, 'Desktop_Agent1', 'failed', 'Desktop_Agent1', ?, ?)",
                (f"run-{uuid.uuid4().hex[:12]}", task_id, one_day_ago, one_day_ago),
            )
            conn.commit()
        finally:
            conn.close()

        result = vault_briefing_digest(include_inbox=False)

        assert "dispatch 90%" in result["delivery_slo_line"]
        assert result["delivery_slo"]["dispatch_success_rate"]["numerator"] == 9
        assert result["delivery_slo"]["dispatch_success_rate"]["denominator"] == 10

    def test_computation_failure_degrades_without_raising(self, vault, monkeypatch):
        import unified_agent.monitor as monitor_mod

        def boom(window_days=7.0):
            raise OSError("delivery_slo storage unreachable")

        monkeypatch.setattr(monitor_mod, "delivery_slo", boom)
        result = vault_briefing_digest(include_inbox=False)
        assert result["delivery_slo"] is None
        assert result["delivery_slo_line"].startswith("Delivery SLO: unavailable")


class TestBriefingDigestInboxTriage:
    """A briefing that lists the inbox without consulting the ingest ledger
    re-proposes work already done — the live 2026-07-28 briefing offered to
    promote a capture wikified the day before (task #204). The digest carries the
    ledger state so its consumers stop guessing.
    """

    def _capture(self, vault, name: str, body: str = "capture body\n") -> str:
        (vault / "00 - Inbox" / name).write_text(body)
        return f"00 - Inbox/{name}"

    def test_reports_ingest_state_per_capture(self, vault, isolated_env):
        fresh = self._capture(vault, "fresh.md")
        done = self._capture(vault, "done.md")
        vault_ingest_status(done, mark=True, agent="Desktop_Agent1", pages=["Wiki/Thing.md"])

        by_path = {e["path"]: e for e in vault_briefing_digest()["inbox"]}

        assert by_path[fresh]["status"] == "new"
        assert by_path[fresh]["last_ingested_at"] is None
        assert by_path[done]["status"] == "unchanged"
        assert by_path[done]["pages"] == ["Wiki/Thing.md"]
        assert by_path[done]["agent"] == "Desktop_Agent1"
        assert by_path[done]["last_ingested_at"] is not None

    def test_edited_capture_reads_as_changed(self, vault, isolated_env):
        rel = self._capture(vault, "edited.md")
        vault_ingest_status(rel, mark=True, agent="Desktop_Agent1", pages=["Wiki/Thing.md"])
        (vault / "00 - Inbox" / "edited.md").write_text("capture body\n\nplus a new paragraph\n")

        entry = next(e for e in vault_briefing_digest()["inbox"] if e["path"] == rel)
        assert entry["status"] == "changed"

    def test_pending_count_excludes_ingested_and_empty_captures(self, vault, isolated_env):
        self._capture(vault, "one.md")
        self._capture(vault, "two.md")
        self._capture(vault, "blank.md", body="   \n")
        done = self._capture(vault, "done.md")
        vault_ingest_status(done, mark=True, agent="Desktop_Agent1", pages=[])

        result = vault_briefing_digest()
        # one.md + two.md await a steward; blank.md is not work, done.md is done.
        assert result["inbox_pending_count"] == 2
        assert {e["status"] for e in result["inbox"]} == {"new", "empty", "unchanged"}

    def test_only_the_inbox_is_walked_and_only_notes(self, vault, isolated_env):
        self._capture(vault, "capture.md")
        (vault / "00 - Inbox" / "image.png").write_bytes(b"\x89PNG not a note")
        (vault / "00 - Inbox" / "Laptop").mkdir()
        (vault / "00 - Inbox" / "Laptop" / "nested.md").write_text("laptop capture\n")

        paths = [e["path"] for e in vault_briefing_digest()["inbox"]]
        assert paths == ["00 - Inbox/capture.md"], "non-notes and subdirs stay out"

    def test_missing_inbox_directory_is_not_an_error(self, vault, isolated_env):
        for f in (vault / "00 - Inbox").iterdir():
            f.unlink()
        (vault / "00 - Inbox").rmdir()

        result = vault_briefing_digest()
        assert result["inbox"] == []
        assert result["inbox_pending_count"] == 0

    def test_opt_out_keeps_the_payload_journal_only(self, vault, isolated_env):
        self._capture(vault, "capture.md")

        result = vault_briefing_digest(include_inbox=False)
        assert "inbox" not in result
        assert "inbox_pending_count" not in result

    def test_reporting_never_marks_a_capture_ingested(self, vault, isolated_env):
        rel = self._capture(vault, "capture.md")

        vault_briefing_digest()
        vault_briefing_digest()

        # Still un-ingested after two reads: the digest reports, it never records.
        assert vault_ingest_status(rel)["status"] == "new"


class TestVaultLockedGuard:
    def _write_locked(self, vault, name="00 - Inbox/locked.md", body="Body text\n"):
        content = f"---\nlocked: true\n---\n\n{body}"
        target = vault / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return content

    def test_locked_overwrite_blocked(self, vault):
        content = self._write_locked(vault)
        with pytest.raises(ToolError, match="locked"):
            vault_write("00 - Inbox/locked.md", "clobbered")
        assert (vault / "00 - Inbox" / "locked.md").read_text() == content

    def test_locked_append_blocked(self, vault):
        content = self._write_locked(vault)
        with pytest.raises(ToolError, match="locked"):
            vault_write("00 - Inbox/locked.md", "more", append=True)
        assert (vault / "00 - Inbox" / "locked.md").read_text() == content

    def test_locked_force_overrides(self, vault):
        self._write_locked(vault)
        vault_write("00 - Inbox/locked.md", "overwritten", force=True)
        assert (vault / "00 - Inbox" / "locked.md").read_text() == "overwritten"

    def test_locked_force_overrides_append(self, vault):
        content = self._write_locked(vault)
        vault_write("00 - Inbox/locked.md", "more", append=True, force=True)
        assert (vault / "00 - Inbox" / "locked.md").read_text() == content + "more"

    def test_unlocked_note_unaffected(self, vault):
        (vault / "00 - Inbox" / "plain.md").write_text("---\ntitle: x\n---\nBody\n")
        vault_write("00 - Inbox/plain.md", "overwritten")
        assert (vault / "00 - Inbox" / "plain.md").read_text() == "overwritten"

    def test_locked_false_unaffected(self, vault):
        (vault / "00 - Inbox" / "unlocked.md").write_text("---\nlocked: false\n---\nBody\n")
        vault_write("00 - Inbox/unlocked.md", "overwritten")
        assert (vault / "00 - Inbox" / "unlocked.md").read_text() == "overwritten"

    def test_new_file_not_blocked_by_guard(self, vault):
        out = vault_write("00 - Inbox/new.md", "hello")
        assert out["bytes_written"] == len(b"hello")


class TestNoteWriteLock:
    """Every hub writer takes one sidecar lock per note path (#1890)."""

    @staticmethod
    def _blocked(fn, *args):
        """Run ``fn`` in a thread; True if it is still waiting after a pause."""
        writer = threading.Thread(target=fn, args=args)
        writer.start()
        writer.join(timeout=0.3)
        return writer.is_alive(), writer

    def test_atomic_replace_does_not_escape_the_lock(self, vault):
        # Sync, git checkout or a temp-file rename swaps the note's inode; a
        # lock on the old inode would no longer fence a writer of the new one.
        note = vault / "00 - Inbox" / "r.md"
        note.write_text("old\n")
        with note_write_lock(note):
            tmp = note.with_suffix(".tmp")
            tmp.write_text("replaced\n")
            os.replace(tmp, note)
            blocked, writer = self._blocked(vault_write, "00 - Inbox/r.md", "hub\n")
            assert blocked, "vault_write escaped the lock after an atomic replace"
        writer.join(timeout=5)
        assert note.read_text() == "hub\n"

    def test_new_note_is_fenced(self, vault):
        note = vault / "00 - Inbox" / "new.md"
        with note_write_lock(note):
            blocked, writer = self._blocked(vault_write, "00 - Inbox/new.md", "x")
            assert blocked
            assert not note.exists()
        writer.join(timeout=5)
        assert note.read_text() == "x"

    def test_journal_takes_the_same_lock(self, vault):
        day = vault / "Logs" / "2026-10-14.md"
        day.write_text("---\ndate: 2026-10-14\n---\n")
        with note_write_lock(day):
            tmp = day.with_suffix(".tmp")
            tmp.write_text("---\ndate: 2026-10-14\n---\n")
            os.replace(tmp, day)
            blocked, writer = self._blocked(vault_journal, "entry", "Desktop_Agent1", "2026-10-14")
            assert blocked, "vault_journal did not take the shared note lock"
        writer.join(timeout=5)
        assert "entry" in day.read_text()

    def test_lock_file_is_private_and_keyed_by_path(self, vault):
        note = vault / "00 - Inbox" / "k.md"
        with note_write_lock(note):
            pass
        lock_dir = config.data_home() / "locks" / "vault"
        key = hashlib.sha256(b"00 - inbox/k.md").hexdigest()
        lock = lock_dir / f"{key}.lock"
        assert lock.is_file()
        assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert not note.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable_note_is_a_tool_error_not_a_write(self, vault):
        # Write-only: the lock check cannot read it, but "wb" would succeed.
        note = vault / "00 - Inbox" / "wo.md"
        note.write_text("---\nlocked: true\n---\nbody\n")
        note.chmod(0o200)
        try:
            with pytest.raises(ToolError, match="cannot read note .*check its lock"):
                vault_write("00 - Inbox/wo.md", "clobbered")
        finally:
            note.chmod(0o600)
        assert note.read_text() == "---\nlocked: true\n---\nbody\n"

    @pytest.mark.parametrize(
        ("text", "locked"),
        [
            ("---\nlocked: true\n---\nbody\n", True),
            ("---\nlocked: false\n---\nbody\n", False),
            ("no frontmatter\n", False),
        ],
    )
    def test_read_reports_the_write_guards_lock_verdict(self, vault, text, locked):
        # Task #2515: the UI shows the verdict vault_write refuses on, not a guess.
        note = vault / "00 - Inbox" / "v.md"
        note.write_text(text)
        assert vault_read("00 - Inbox/v.md")["locked"] is locked
        if locked:
            with pytest.raises(ToolError, match="locked"):
                vault_write("00 - Inbox/v.md", "clobbered")


class TestVaultIngestStatus:
    def test_new_marked_unchanged_changed_cycle(self, vault):
        note = vault / "01 - Projects" / "capture.md"
        note.write_text("Some captured content.\n")

        new = vault_ingest_status("01 - Projects/capture.md")
        assert new["status"] == "new"
        assert new["last_ingested_at"] is None
        assert new["agent"] is None
        assert new["pages"] == []

        marked = vault_ingest_status(
            "01 - Projects/capture.md",
            mark=True,
            agent="Desktop_Agent1",
            pages=["Wiki/Foo.md"],
        )
        assert marked["status"] == "marked"
        assert marked["agent"] == "Desktop_Agent1"
        assert marked["pages"] == ["Wiki/Foo.md"]
        assert marked["content_hash"] == new["content_hash"]
        assert marked["last_ingested_at"] is not None

        unchanged = vault_ingest_status("01 - Projects/capture.md")
        assert unchanged["status"] == "unchanged"
        assert unchanged["agent"] == "Desktop_Agent1"
        assert unchanged["pages"] == ["Wiki/Foo.md"]
        assert unchanged["last_ingested_at"] == marked["last_ingested_at"]

        note.write_text("Some captured content, edited.\n")
        changed = vault_ingest_status("01 - Projects/capture.md")
        assert changed["status"] == "changed"
        assert changed["content_hash"] != new["content_hash"]
        # last-known ingest metadata is still surfaced even though it's stale
        assert changed["agent"] == "Desktop_Agent1"
        assert changed["pages"] == ["Wiki/Foo.md"]

    def test_empty_file_gate(self, vault):
        note = vault / "01 - Projects" / "empty.md"
        note.write_text("   \n\n  ")
        out = vault_ingest_status("01 - Projects/empty.md")
        assert out["status"] == "empty"
        with pytest.raises(ToolError, match="empty"):
            vault_ingest_status("01 - Projects/empty.md", mark=True, agent="Desktop_Agent1")

    def test_mark_without_agent_rejected(self, vault):
        note = vault / "01 - Projects" / "capture2.md"
        note.write_text("content\n")
        with pytest.raises(ToolError, match="agent label"):
            vault_ingest_status("01 - Projects/capture2.md", mark=True)

    def test_missing_file_rejected(self, vault):
        with pytest.raises(ToolError, match="no such note"):
            vault_ingest_status("01 - Projects/nope.md")


class TestVaultAdvancedFeatures:
    def test_read_with_transclusion(self, vault):
        (vault / "01 - Projects" / "embed.md").write_text("Embed content here")
        (vault / "01 - Projects" / "parent.md").write_text("Hello ![[embed]] world")

        # Without expand_embeds
        res1 = vault_read("01 - Projects/parent.md")
        assert res1["content"] == "Hello ![[embed]] world"

        # With expand_embeds
        res2 = vault_read("01 - Projects/parent.md", expand_embeds=True)
        assert "--- [Transclusion: embed] ---" in res2["content"]
        assert "Embed content here" in res2["content"]

    def test_write_with_template(self, vault):
        (vault / "00 - Inbox" / "tpl.md").write_text(
            "# {{title}}\nDate: {{date}}\nContent: {{content}}"
        )

        vault_write(
            "00 - Inbox/templated.md", content="My custom entry", template="00 - Inbox/tpl.md"
        )
        templated_text = (vault / "00 - Inbox" / "templated.md").read_text()
        assert "# templated" in templated_text
        assert "Content: My custom entry" in templated_text
        import datetime

        assert datetime.datetime.now().strftime("%Y-%m-%d") in templated_text

    def test_template_write_still_respects_the_lock(self, vault):
        """A templated write is a write: `locked: true` must still refuse it.

        The transclusion/template work predates the lock guard, so merging the
        two had to keep both — a template path that skipped _is_locked would be
        a way around the write boundary.
        """
        (vault / "00 - Inbox" / "tpl2.md").write_text("# {{title}}\n{{content}}")
        locked = vault / "00 - Inbox" / "locked-note.md"
        locked.write_text("---\nlocked: true\n---\n\noriginal body\n")

        with pytest.raises(ToolError, match="locked"):
            vault_write(
                "00 - Inbox/locked-note.md",
                content="overwrite attempt",
                template="00 - Inbox/tpl2.md",
            )
        assert "original body" in locked.read_text()

        vault_write(
            "00 - Inbox/locked-note.md",
            content="approved overwrite",
            template="00 - Inbox/tpl2.md",
            force=True,
        )
        assert "approved overwrite" in locked.read_text()


class TestVaultMove:
    def _para_vault(self, tmp_path, monkeypatch):
        root = tmp_path / "vault"
        for d in ("00 - Inbox", "01 - Projects", "04 - Archive", "Logs", "Wiki"):
            (root / d).mkdir(parents=True)
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        return root

    def test_forbidden_without_cos_profile(self, vault):
        (vault / "00 - Inbox" / "a.md").write_text("content")
        with pytest.raises(ToolError, match="chief-of-staff"):
            vault_move("00 - Inbox/a.md", "Logs/a.md")
        assert (vault / "00 - Inbox" / "a.md").exists()

    def test_cos_moves_inbox_to_archive_and_creates_subfolder(
        self, tmp_path, monkeypatch, cos_session
    ):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "capture.md").write_text("captured content")

        out = vault_move("00 - Inbox/capture.md", "04 - Archive/2026-08/capture.md")

        assert out == {
            "source": "00 - Inbox/capture.md",
            "destination": "04 - Archive/2026-08/capture.md",
            "bytes_moved": len(b"captured content"),
        }
        assert not (root / "00 - Inbox" / "capture.md").exists()
        assert (root / "04 - Archive" / "2026-08" / "capture.md").read_text() == "captured content"

    def test_forbidden_traversal_destination(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("x")
        with pytest.raises(ToolError, match="escapes the vault"):
            vault_move("00 - Inbox/a.md", "../outside.md")
        assert (root / "00 - Inbox" / "a.md").exists()

    def test_missing_source_rejected(self, tmp_path, monkeypatch, cos_session):
        self._para_vault(tmp_path, monkeypatch)
        with pytest.raises(ToolError, match="no such note"):
            vault_move("00 - Inbox/nope.md", "Logs/nope.md")

    def test_locked_source_blocked_without_force(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        locked = root / "00 - Inbox" / "locked.md"
        locked.write_text("---\nlocked: true\n---\n\nbody\n")
        with pytest.raises(ToolError, match="locked"):
            vault_move("00 - Inbox/locked.md", "Logs/locked.md")
        assert locked.exists()

    def test_locked_source_force_overrides(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "locked.md").write_text("---\nlocked: true\n---\n\nbody\n")
        vault_move("00 - Inbox/locked.md", "Logs/locked.md", force=True)
        assert not (root / "00 - Inbox" / "locked.md").exists()
        assert (root / "Logs" / "locked.md").exists()

    def test_existing_destination_blocked_without_force(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("new")
        (root / "Logs" / "a.md").write_text("existing")
        with pytest.raises(ToolError, match="already exists"):
            vault_move("00 - Inbox/a.md", "Logs/a.md")
        assert (root / "Logs" / "a.md").read_text() == "existing"
        assert (root / "00 - Inbox" / "a.md").exists()

    def test_existing_destination_force_overwrites(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("new")
        (root / "Logs" / "a.md").write_text("existing")
        vault_move("00 - Inbox/a.md", "Logs/a.md", force=True)
        assert (root / "Logs" / "a.md").read_text() == "new"
        assert not (root / "00 - Inbox" / "a.md").exists()

    def test_same_source_and_destination_rejected(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("x")
        with pytest.raises(ToolError, match="same path"):
            vault_move("00 - Inbox/a.md", "00 - Inbox/a.md")

    def test_destination_directory_rejected(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("x")
        (root / "Logs" / "dir.md").mkdir()
        with pytest.raises(ToolError, match="directory"):
            vault_move("00 - Inbox/a.md", "Logs/dir.md")

    def test_non_markdown_destination_rejected(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "00 - Inbox" / "a.md").write_text("x")
        with pytest.raises(ToolError, match="limited to"):
            vault_move("00 - Inbox/a.md", "Logs/a.sh")

    def test_move_out_of_allowed_dirs_rejected(self, tmp_path, monkeypatch, cos_session):
        """Even chief-of-staff can't move into a dir outside its widened allowlist."""
        root = self._para_vault(tmp_path, monkeypatch)
        (root / "unscoped").mkdir()
        (root / "00 - Inbox" / "a.md").write_text("x")
        with pytest.raises(ToolError, match="restricted to"):
            vault_move("00 - Inbox/a.md", "unscoped/a.md")


class TestVaultMoveNoteLock:
    """vault_move takes the per-note lock on source and destination (#2058)."""

    @staticmethod
    def _para_vault(tmp_path, monkeypatch):
        root = tmp_path / "vault"
        for d in ("00 - Inbox", "04 - Archive", "Logs"):
            (root / d).mkdir(parents=True)
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        return root

    @staticmethod
    def _start(fn, *args, **kwargs):
        """Run ``fn`` in a daemon thread under the caller's context (profile, identity)."""
        ctx = contextvars.copy_context()
        outcome: dict = {}

        def run():
            try:
                outcome["result"] = ctx.run(fn, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, outcome

    def _blocked(self, fn, *args, **kwargs):
        worker, outcome = self._start(fn, *args, **kwargs)
        worker.join(timeout=0.3)
        return worker.is_alive(), worker, outcome

    def test_source_lock_fences_the_move(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        src = root / "00 - Inbox" / "a.md"
        src.write_text("body\n")
        with note_write_lock(src):
            blocked, worker, outcome = self._blocked(vault_move, "00 - Inbox/a.md", "Logs/a.md")
            assert blocked, "vault_move did not wait for the source note lock"
            assert src.exists()
        worker.join(timeout=5)
        assert "error" not in outcome, outcome
        assert (root / "Logs" / "a.md").read_text() == "body\n"

    def test_destination_lock_fences_the_move(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        src = root / "00 - Inbox" / "a.md"
        src.write_text("body\n")
        dst = root / "Logs" / "a.md"
        with note_write_lock(dst):
            blocked, worker, outcome = self._blocked(vault_move, "00 - Inbox/a.md", "Logs/a.md")
            assert blocked, "vault_move did not wait for the destination note lock"
            assert not dst.exists()
        worker.join(timeout=5)
        assert "error" not in outcome, outcome
        assert dst.read_text() == "body\n"

    def test_lock_guard_is_checked_under_the_note_lock(self, tmp_path, monkeypatch, cos_session):
        # A writer holding the lock marks the source locked; the waiting move must
        # see that guard, not the unlocked note it saw before it queued.
        root = self._para_vault(tmp_path, monkeypatch)
        src = root / "00 - Inbox" / "a.md"
        src.write_text("body\n")
        with note_write_lock(src):
            blocked, worker, outcome = self._blocked(vault_move, "00 - Inbox/a.md", "Logs/a.md")
            assert blocked
            src.write_text("---\nlocked: true\n---\nbody\n")
        worker.join(timeout=5)
        assert isinstance(outcome.get("error"), ToolError), outcome
        assert "locked" in str(outcome["error"])
        assert src.exists()
        assert not (root / "Logs" / "a.md").exists()

    def test_destination_created_while_waiting_is_not_clobbered(
        self, tmp_path, monkeypatch, cos_session
    ):
        root = self._para_vault(tmp_path, monkeypatch)
        src = root / "00 - Inbox" / "a.md"
        src.write_text("mover\n")
        dst = root / "Logs" / "a.md"
        with note_write_lock(dst):
            blocked, worker, outcome = self._blocked(vault_move, "00 - Inbox/a.md", "Logs/a.md")
            assert blocked
            dst.write_text("writer\n")
        worker.join(timeout=5)
        assert isinstance(outcome.get("error"), ToolError), outcome
        assert "already exists" in str(outcome["error"])
        assert dst.read_text() == "writer\n"
        assert src.read_text() == "mover\n"

    def test_locks_are_taken_in_one_order_whatever_the_direction(
        self, tmp_path, monkeypatch, cos_session
    ):
        root = self._para_vault(tmp_path, monkeypatch)
        a = root / "00 - Inbox" / "a.md"
        b = root / "Logs" / "b.md"
        order: list[str] = []
        real = note_lock._hold_note_lock_file

        @contextlib.contextmanager
        def recording(name):
            order.append(name)
            with real(name):
                yield

        monkeypatch.setattr(note_lock, "_hold_note_lock_file", recording)
        a.write_text("x")
        vault_move("00 - Inbox/a.md", "Logs/b.md")
        forward = list(order)
        order.clear()
        vault_move("Logs/b.md", "00 - Inbox/a.md")
        backward = list(order)
        assert len(forward) == 2
        assert forward == backward == sorted(forward)
        assert a.read_text() == "x" and not b.exists()

    def test_crossing_moves_do_not_deadlock(self, tmp_path, monkeypatch, cos_session):
        root = self._para_vault(tmp_path, monkeypatch)
        a = root / "00 - Inbox" / "a.md"
        b = root / "Logs" / "b.md"
        a.write_text("a\n")
        b.write_text("b\n")
        # Pause each move once it holds its first lock, so both are mid-acquire
        # together: unsorted acquisition (src then dst) deadlocks here.
        real = note_lock._hold_note_lock_file
        gate = threading.Barrier(2, timeout=1)

        @contextlib.contextmanager
        def slow(name):
            with real(name):
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
                yield

        monkeypatch.setattr(note_lock, "_hold_note_lock_file", slow)
        w1, o1 = self._start(vault_move, "00 - Inbox/a.md", "Logs/b.md", force=True)
        w2, o2 = self._start(vault_move, "Logs/b.md", "00 - Inbox/a.md", force=True)
        w1.join(timeout=10)
        w2.join(timeout=10)
        assert not w1.is_alive() and not w2.is_alive(), "crossing vault_move calls deadlocked"
        # Serialized: whichever order they ran in, one note is left and a failed
        # move failed only because the other one took its source first.
        for outcome in (o1, o2):
            if "error" in outcome:
                assert "no such note" in str(outcome["error"]), outcome
        assert a.exists() != b.exists()

    def test_case_only_rename_does_not_self_deadlock(self, tmp_path, monkeypatch, cos_session):
        # a.md and A.md share one case-folded lock key; taking it twice from one
        # call would block on itself. Holds on case-sensitive (Linux) and
        # case-insensitive (macOS APFS) volumes alike.
        root = self._para_vault(tmp_path, monkeypatch)
        inbox = root / "00 - Inbox"
        probe = inbox / "case-probe.md"
        probe.write_text("")
        case_insensitive = (inbox / "CASE-PROBE.md").exists()
        probe.unlink()

        lower, upper = inbox / "a.md", inbox / "A.md"
        lower.write_text("x")

        # The lock primitive itself, independent of what the volume makes of
        # the two names: both spellings take one lock, once.
        def take_both():
            with note_write_locks(lower, upper):
                return True

        worker, outcome = self._start(take_both)
        worker.join(timeout=5)
        assert not worker.is_alive(), "note_write_locks deadlocked on a shared key"
        assert outcome == {"result": True}, outcome

        worker, outcome = self._start(vault_move, "00 - Inbox/a.md", "00 - Inbox/A.md")
        worker.join(timeout=5)
        assert not worker.is_alive(), "vault_move deadlocked on its own note lock"
        names = {p.name for p in inbox.iterdir()}
        if case_insensitive:
            # A.md already names the source, so the unforced move refuses it
            # (as an existing destination, or as the same path if the resolver
            # canonicalizes case) and the note is untouched.
            assert isinstance(outcome.get("error"), ToolError), outcome
            assert re.search("already exists|same path", str(outcome["error"])), outcome
            assert lower.read_text() == "x"
        else:
            assert "error" not in outcome, outcome
            assert names == {"A.md"}
            assert upper.read_text() == "x"


class TestVaultArchivePlan:
    def test_plan_is_read_only_and_separates_final_promotion(self, vault):
        final = vault / "00 - Inbox" / "final.md"
        final.write_text(
            "---\nstatus: final\n---\n\nBody\n\nprocessed → [[Final]] (Desktop_Agent2)\n"
        )
        draft = vault / "00 - Inbox" / "draft.md"
        draft.write_text("---\nstatus: proposal\n---\n\nBody\n\nprocessed → no Wiki page\n")
        pending = vault / "00 - Inbox" / "pending.md"
        pending.write_text("# Pending\n")

        vault_ingest_status("00 - Inbox/final.md", mark=True, agent="Desktop_Agent2")
        vault_ingest_status("00 - Inbox/draft.md", mark=True, agent="Desktop_Agent2")

        out = vault_archive_plan(as_of="2026-08-08")

        assert out["read_only"] is True
        assert out["archive_root"] == "04 - Archive/inbox-archive/2026-08-08"
        by_source = {row["source"]: row for row in out["candidates"]}
        assert by_source["00 - Inbox/final.md"]["action"] == "move"
        assert by_source["00 - Inbox/final.md"]["promotion"] == "authoritative"
        assert by_source["00 - Inbox/draft.md"]["classification"] == "archive-only"
        assert by_source["00 - Inbox/draft.md"]["promotion"] == "none"
        assert by_source["00 - Inbox/pending.md"]["action"] == "hold"
        assert final.exists() and draft.exists() and pending.exists()

        shipped = vault / "00 - Inbox" / "shipped.md"
        shipped.write_text(
            "---\nimplementation_status: shipped\n---\n\nBody\n\nprocessed → [[Shipped]]\n"
        )
        vault_ingest_status("00 - Inbox/shipped.md", mark=True, agent="Desktop_Agent2")
        planned = {
            row["source"]: row for row in vault_archive_plan(as_of="2026-08-08")["candidates"]
        }
        assert planned["00 - Inbox/shipped.md"]["shipped"] is True
        assert planned["00 - Inbox/shipped.md"]["promotion"] == "authoritative"

    def test_superseded_capture_can_file_without_wiki_promotion(self, vault):
        note = vault / "00 - Inbox" / "old.md"
        note.write_text("---\nsuperseded_by: Newer Note\n---\n\nOld\n")

        out = vault_archive_plan(as_of="2026-08-08")

        row = out["candidates"][0]
        assert row["action"] == "move"
        assert row["classification"] == "archive-only"
        assert row["promotion"] == "none"
        assert row["superseded_target"] == "Newer Note"


class TestLedgerAnchorDirReserved:
    """Task #1908: Logs/audit-ledger-heads/ holds the chain-head witnesses
    written by audit/reanchor.py. An agent tool must not write, overwrite or
    move into it, even with force=True (the notes are locked, and force is the
    lock override). Only the reanchor path may write there."""

    ANCHOR = "Logs/audit-ledger-heads/2026-09-06.md"

    def _seed(self, vault):
        note = vault / self.ANCHOR
        note.parent.mkdir(parents=True)
        original = "---\ntype: audit-ledger-head\nlocked: true\n---\n\n- head_hash: abc\n"
        note.write_text(original)
        return note, original

    def test_force_overwrite_refused(self, vault):
        note, original = self._seed(vault)
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            vault_write(self.ANCHOR, "- head_hash: forged\n", force=True)
        assert note.read_text() == original

    def test_force_append_refused(self, vault):
        note, original = self._seed(vault)
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            vault_write(self.ANCHOR, "- head_hash: forged\n", mode="append", force=True)
        assert note.read_text() == original

    def test_new_note_refused(self, vault):
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            vault_write("Logs/audit-ledger-heads/2030-01-01.md", "- head_hash: forged\n")
        assert not (vault / "Logs" / "audit-ledger-heads" / "2030-01-01.md").exists()

    def test_case_variant_refused(self, vault):
        # APFS (the hub's filesystem) is case-insensitive by default.
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            vault_write("Logs/Audit-Ledger-Heads/2030-01-01.md", "x", force=True)

    def test_resolver_refuses_without_internal_flag(self, vault):
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            resolve_in_vault(self.ANCHOR, for_write=True)
        assert resolve_in_vault(self.ANCHOR) == (vault / self.ANCHOR).resolve()
        assert (
            resolve_in_vault(self.ANCHOR, for_write=True, for_ledger_anchor=True)
            == (vault / self.ANCHOR).resolve()
        )

    def test_move_into_anchor_dir_refused(self, vault, cos_session):
        self._seed(vault)
        (vault / "00 - Inbox" / "forged.md").write_text("- head_hash: forged\n")
        with pytest.raises(ToolError, match="audit-ledger-heads"):
            vault_move("00 - Inbox/forged.md", self.ANCHOR, force=True)
        assert (vault / "00 - Inbox" / "forged.md").exists()

    def test_reanchor_still_writes(self, vault, tmp_path, monkeypatch):
        from unified_agent.audit.reanchor import reanchor_chain_head

        monkeypatch.setattr(config, "db_path", lambda: tmp_path / "memory.db")
        storage.init_db()
        result = reanchor_chain_head(day="2026-09-07")
        assert result["anchor_action"] == "created"
        assert (vault / "Logs" / "audit-ledger-heads" / "2026-09-07.md").is_file()
