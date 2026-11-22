"""The hub's generated-note writers respect frontmatter ``locked: true`` (#2091).

consolidate, sleep_pass, evolve, harness_config, merge's failure report and the
audit reanchor write vault notes directly rather than through ``vault_write``,
so before #2091 a note a human had locked was overwritten by the next run. Each
writer now checks the lock under the note lock and, finding the note locked,
either writes the next free sibling (``<stem>-2.md``, ``<stem>-3.md``...) and
reports that path, or refuses. The per-writer choice is documented at each call
site; these tests pin it.

The last tests cover the check-then-write races in ``tools/learning.py``
(the existing-proposal fingerprint check ran outside the lock ``vault_write``
takes) and ``tools/export.py`` (a ``while p.exists()`` name bump followed by an
unguarded ``write_bytes``; exports now publish with a no-clobber hard link,
#2094, covered in full in ``tests/test_export.py``).
"""

from __future__ import annotations

import contextvars
import pathlib
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_agent import (
    config,
    consolidate,
    evolve,
    harness_config,
    merge,
    note_lock,
    sleep_pass,
    storage,
)
from unified_agent.safety import VaultLockedError
from unified_agent.tools import export as export_tools
from unified_agent.tools import learning
from unified_agent.tools.memory import memory_save
from unified_agent.tools.registry import session_end, session_start

_STAMP = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
LOCKED = "---\nlocked: true\n---\nHUMAN-CURATED\n"


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _STAMP if tz is not None else _STAMP.replace(tzinfo=None)


@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "Logs"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    return root.resolve()


def _lock(path: Path, text: str = LOCKED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the shared slot helper -------------------------------------------------


def test_slot_is_the_target_when_it_is_not_locked(vault):
    target = vault / "00 - Inbox" / "Report.md"
    target.write_text("---\ntitle: plain\n---\nold\n", encoding="utf-8")
    with note_lock.unlocked_note_slot(target) as slot:
        assert slot == target


def test_slot_skips_every_locked_name_in_order(vault):
    target = _lock(vault / "00 - Inbox" / "Report.md")
    _lock(vault / "00 - Inbox" / "Report-2.md")
    with note_lock.unlocked_note_slot(target) as slot:
        assert slot == vault / "00 - Inbox" / "Report-3.md"


def test_slot_refuses_when_no_sibling_is_free(vault):
    target = _lock(vault / "00 - Inbox" / "Report.md")
    _lock(vault / "00 - Inbox" / "Report-2.md")
    with pytest.raises(VaultLockedError), note_lock.unlocked_note_slot(target, max_siblings=1):
        pass


def test_slot_refuses_when_siblings_are_off(vault):
    target = _lock(vault / "00 - Inbox" / "Report.md")
    with pytest.raises(VaultLockedError), note_lock.unlocked_note_slot(target, max_siblings=0):
        pass


# --- consolidate and sleep_pass: per-run reports, write a sibling ----------


def test_consolidate_locked_proposal_writes_a_sibling(vault, monkeypatch):
    monkeypatch.setattr(consolidate, "datetime", _FixedDatetime)
    target = _lock(vault / "00 - Inbox" / "Memory-Consolidation-Proposal-20261014-120000.md")
    result = consolidate._write_artifact("# proposal\n")
    sibling = target.with_name("Memory-Consolidation-Proposal-20261014-120000-2.md")
    assert target.read_text(encoding="utf-8") == LOCKED
    assert Path(result) == sibling
    assert sibling.read_text(encoding="utf-8") == "# proposal\n"


def test_sleep_pass_locked_report_writes_a_sibling(vault, monkeypatch):
    monkeypatch.setattr(sleep_pass, "datetime", _FixedDatetime)
    target = _lock(vault / "00 - Inbox" / "Sleep-Pass-20261014-120000.md")
    result = sleep_pass._write_artifact("# sleep\n")
    sibling = target.with_name("Sleep-Pass-20261014-120000-2.md")
    assert target.read_text(encoding="utf-8") == LOCKED
    assert Path(result) == sibling
    assert sibling.read_text(encoding="utf-8") == "# sleep\n"


# --- evolve: the rolling proposal note, write a sibling --------------------


def test_evolve_locked_proposal_writes_a_sibling(vault):
    target = _lock(vault / evolve.PROPOSAL_RELPATH)
    result = evolve._write_artifact("# skills\n", fingerprint="abc123")
    sibling = target.with_name("Skill-Cluster-Proposals-2.md")
    assert target.read_text(encoding="utf-8") == LOCKED
    assert Path(result) == sibling
    assert sibling.read_text(encoding="utf-8") == "# skills\n"


def test_evolve_locked_proposal_holding_this_fingerprint_is_unchanged(vault):
    # The human locked the note that already holds this exact proposal: there
    # is nothing new to file, so no sibling appears.
    target = _lock(vault / evolve.PROPOSAL_RELPATH, "---\nlocked: true\nfingerprint: abc123\n---\n")
    result = evolve._write_artifact("# skills\n", fingerprint="abc123")
    assert result is evolve._UNCHANGED
    assert not target.with_name("Skill-Cluster-Proposals-2.md").exists()


def _two_related_trusted_notes() -> None:
    for content, title in (
        ("Always call task_claim before editing unified-agent product files.", "Claim first"),
        ("Never edit unified-agent product files until task_claim succeeds.", "No edit unclaimed"),
    ):
        memory_save(
            content,
            title=title,
            tags=["fact", "project:unified-agent"],
            confidence=0.9,
            author="Desktop_Agent4",
            salience=0.8,
        )


def test_evolve_pass_reports_the_sibling_and_the_locked_note(vault):
    _two_related_trusted_notes()
    target = _lock(vault / evolve.PROPOSAL_RELPATH)
    result = evolve.run_evolve_pass(write=True)
    assert result["written"] is True
    assert result["path"] == "00 - Inbox/Skill-Cluster-Proposals-2.md"
    assert result["locked_path"] == evolve.PROPOSAL_RELPATH
    assert target.read_text(encoding="utf-8") == LOCKED
    assert "skill-cluster-proposal" in (vault / result["path"]).read_text(encoding="utf-8")


def _fp_note(fp: str, *, locked: bool = True) -> str:
    lock = "locked: true\n" if locked else ""
    return f"---\ntype: skill-cluster-proposal\n{lock}fingerprint: {fp}\n---\nBODY {fp}\n"


def test_evolve_finds_its_proposal_in_a_locked_intermediate_sibling(vault):
    # Original locked with A, -2 locked with B; filing B again must find -2,
    # not write a third copy to -3 (the pane would list both).
    target = _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    _lock(target.with_name("Skill-Cluster-Proposals-2.md"), _fp_note("B"))
    outcome, note = evolve._file_artifact("# skills\n", fingerprint="B")
    assert outcome is evolve._UNCHANGED
    assert note == target.with_name("Skill-Cluster-Proposals-2.md")
    assert not target.with_name("Skill-Cluster-Proposals-3.md").exists()


def test_evolve_finds_its_proposal_in_a_locked_sibling_past_a_free_original(vault):
    # The original is unlocked again but a locked sibling from earlier holds B.
    target = vault / evolve.PROPOSAL_RELPATH
    target.write_text(_fp_note("A", locked=False), encoding="utf-8")
    _lock(target.with_name("Skill-Cluster-Proposals-2.md"), _fp_note("B"))
    outcome, note = evolve._file_artifact("# skills\n", fingerprint="B")
    assert outcome is evolve._UNCHANGED
    assert note == target.with_name("Skill-Cluster-Proposals-2.md")
    assert target.read_text(encoding="utf-8") == _fp_note("A", locked=False)


def test_evolve_finds_its_proposal_when_every_candidate_is_locked(vault, monkeypatch):
    monkeypatch.setattr(note_lock, "MAX_LOCKED_SIBLINGS", 1)
    target = _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    _lock(target.with_name("Skill-Cluster-Proposals-2.md"), _fp_note("B"))
    assert evolve._write_artifact("# skills\n", fingerprint="A") is evolve._UNCHANGED
    fallback = config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name
    assert not fallback.exists()


def test_evolve_every_candidate_locked_takes_the_report_file_fallback(vault, monkeypatch):
    _two_related_trusted_notes()
    monkeypatch.setattr(note_lock, "MAX_LOCKED_SIBLINGS", 1)
    target = _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    _lock(target.with_name("Skill-Cluster-Proposals-2.md"), _fp_note("B"))
    result = evolve.run_evolve_pass(write=True)
    fallback = config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name
    assert result["written"] is True
    assert result["path"] == str(fallback)
    assert fallback.is_file()
    assert not target.with_name("Skill-Cluster-Proposals-3.md").exists()


def test_evolve_unreadable_sibling_takes_the_report_file_fallback(vault):
    # A sibling whose lock cannot be read (here a directory with the note's
    # name) makes the slot scan raise ToolError; the pass must fall back to the
    # report file, not die.
    _two_related_trusted_notes()
    target = _lock(vault / evolve.PROPOSAL_RELPATH)
    target.with_name("Skill-Cluster-Proposals-2.md").mkdir()
    result = evolve.run_evolve_pass(write=True)
    fallback = config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name
    assert result["written"] is True
    assert result["path"] == str(fallback)
    assert target.read_text(encoding="utf-8") == LOCKED


def _deny_read(monkeypatch, name: str) -> None:
    """Make ``Path.read_text`` on the note called ``name`` fail as chmod 000 would."""
    real = pathlib.Path.read_text

    def read_text(self, *a, **k):
        if self.name == name:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)


def test_evolve_unreadable_regular_sibling_takes_the_report_file_fallback(vault, monkeypatch):
    # A sibling we cannot read might already hold this proposal, so filing to
    # the next name could duplicate it; the pass files to the off-vault report
    # instead of aborting (round 2: a regular file, not a directory).
    _two_related_trusted_notes()
    target = _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    sibling = target.with_name("Skill-Cluster-Proposals-2.md")
    sibling.write_text(_fp_note("B"), encoding="utf-8")
    _deny_read(monkeypatch, sibling.name)
    result = evolve.run_evolve_pass(write=True)
    fallback = config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name
    assert result["written"] is True
    assert result["path"] == str(fallback)
    assert not target.with_name("Skill-Cluster-Proposals-3.md").exists()


def test_evolve_unreadable_original_proposal_still_fails_the_pass(vault, monkeypatch):
    # The #2086 contract: a filed original we cannot compare against fails the
    # pass rather than being filed again anywhere.
    _two_related_trusted_notes()
    target = vault / evolve.PROPOSAL_RELPATH
    target.write_text(_fp_note("A", locked=False), encoding="utf-8")
    _deny_read(monkeypatch, target.name)
    with pytest.raises(PermissionError):
        evolve.run_evolve_pass(write=True)
    assert not target.with_name("Skill-Cluster-Proposals-2.md").exists()
    assert not (config.data_home() / "reports" / target.name).exists()


def _bare_result(fingerprint: str) -> dict:
    return {"markdown": "# skills\n", "fingerprint": fingerprint, "dry_run": False}


def test_evolve_locked_path_is_omitted_when_the_original_is_not_locked(vault):
    target = vault / evolve.PROPOSAL_RELPATH
    target.write_text(_fp_note("A", locked=False), encoding="utf-8")
    _lock(target.with_name("Skill-Cluster-Proposals-2.md"), _fp_note("B"))
    result = evolve.file_proposal(_bare_result("B"))
    assert result["existing"] is True
    assert result["path"] == "00 - Inbox/Skill-Cluster-Proposals-2.md"
    assert "locked_path" not in result


def test_evolve_locked_path_names_the_locked_original(vault):
    _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    result = evolve.file_proposal(_bare_result("B"))
    assert result["written"] is True
    assert result["path"] == "00 - Inbox/Skill-Cluster-Proposals-2.md"
    assert result["locked_path"] == evolve.PROPOSAL_RELPATH


def test_evolve_scan_and_placement_are_one_critical_section(vault, monkeypatch):
    # Codex round 2: W1 finishes its fingerprint scan and pauses before placing;
    # W2 files B to -2; an ordinary write then locks -2; W1 resumes. Without one
    # lock across scan and placement, W1 skipped the now-locked -2 and filed B
    # again to -3. Now W2 queues behind W1, and B is filed once.
    target = _lock(vault / evolve.PROPOSAL_RELPATH, _fp_note("A"))
    sibling = target.with_name("Skill-Cluster-Proposals-2.md")
    third = target.with_name("Skill-Cluster-Proposals-3.md")
    paused, resume = threading.Event(), threading.Event()
    w1_ident: list[int] = []
    real_exists = pathlib.Path.exists

    def exists(self, *a, **k):
        found = real_exists(self, *a, **k)
        if self == sibling and w1_ident == [threading.get_ident()] and not paused.is_set():
            # W1's scan has seen everything; hold it before placement.
            paused.set()
            assert resume.wait(timeout=30)
        return found

    monkeypatch.setattr(pathlib.Path, "exists", exists)
    outcomes: dict[str, object] = {}

    def w1():
        w1_ident.append(threading.get_ident())
        outcomes["w1"] = evolve._file_artifact(_fp_note("B", locked=False), fingerprint="B")

    def w2():
        outcomes["w2"] = evolve._file_artifact(_fp_note("B", locked=False), fingerprint="B")

    t1 = threading.Thread(target=w1, daemon=True)
    t1.start()
    assert paused.wait(timeout=30), "W1 never reached the end of its scan"
    t2 = threading.Thread(target=w2, daemon=True)
    t2.start()
    t2.join(timeout=0.5)  # an unserialized W2 files B to -2 in this window
    if real_exists(sibling):
        with note_lock.note_write_lock(sibling):
            sibling.write_text(_fp_note("B"), encoding="utf-8")  # now locked, still B
    resume.set()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive()
    assert not real_exists(third), "B was filed twice"
    holders = [
        p
        for p in sorted((vault / "00 - Inbox").glob("Skill-Cluster-Proposals*.md"))
        if "fingerprint: B" in p.read_text(encoding="utf-8")
    ]
    assert holders == [sibling]
    assert {outcomes["w1"][0], outcomes["w2"][0]} == {str(sibling), evolve._UNCHANGED}


def test_slot_names_run_from_dash_2_to_max_siblings_plus_1(vault):
    target = _lock(vault / "00 - Inbox" / "Report.md")
    for n in range(2, 4):
        _lock(vault / "00 - Inbox" / f"Report-{n}.md")
    with note_lock.unlocked_note_slot(target, max_siblings=3) as slot:
        assert slot.name == "Report-4.md"


# --- harness_config: per-proposal note, write a sibling --------------------


def test_harness_config_locked_proposal_writes_a_sibling(vault):
    target = _lock(vault / "00 - Inbox" / "harness-config" / "desk-claude-v1.md")
    rel = harness_config._write_inbox_proposal(
        node="desk",
        surface="claude",
        version="v1",
        path="~/.claude.json",
        diff="+x\n",
        proposal_id="hc-2",
    )
    assert rel == "00 - Inbox/harness-config/desk-claude-v1-2.md"
    assert target.read_text(encoding="utf-8") == LOCKED
    assert "hc-2" in (vault / rel).read_text(encoding="utf-8")


# --- merge: latest failure report, write a sibling -------------------------


def test_merge_locked_failure_report_writes_a_sibling(vault):
    target = _lock(vault / "00 - Inbox" / "Merge-Failed-Task-42.md")
    report = merge.write_failed_report(42, "tests failed", "1 failed", "")
    assert report == target.with_name("Merge-Failed-Task-42-2.md")
    assert target.read_text(encoding="utf-8") == LOCKED
    assert "Task 42" in report.read_text(encoding="utf-8")


# --- reanchor: its own witness is append-only; a foreign lock refuses -------


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()


def test_reanchor_refuses_a_locked_note_it_did_not_write(vault, ledger_db):
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    target = _lock(vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md")
    with pytest.raises(VaultLockedError, match="locked"):
        reanchor_chain_head(day="2026-10-14")
    assert target.read_text(encoding="utf-8") == LOCKED


def _witness(head_hash: str = "earlier", *, stamp: str = "2026-10-14") -> str:
    """The exact note ``reanchor_chain_head`` creates for ``stamp``."""
    from unified_agent.audit.reanchor import _snapshot_note

    report = {
        "status": "ok",
        "verified": True,
        "total_verified": 1,
        "head_hash": head_hash,
        "head_ulid": "01TEST",
    }
    return _snapshot_note(report, stamp=stamp, anchored_at="2026-10-14T00:00:00+00:00")


def test_reanchor_appends_to_its_own_locked_witness(vault, ledger_db):
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    witness = _witness()
    target = _lock(vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md", witness)
    result = reanchor_chain_head(day="2026-10-14")
    assert result["anchor_action"] == "appended"
    text = target.read_text(encoding="utf-8")
    assert text.startswith(witness)
    assert result["head_hash"] in text


def test_reanchor_refuses_a_locked_note_carrying_only_the_type_marker(vault, ledger_db):
    # The type line alone is not the witness shape: a human note that borrows
    # it, without the snapshot's date, heading and head hash, is not appended to.
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    note = "---\ntype: audit-ledger-head\nlocked: true\n---\n\nmy own notes\n"
    target = _lock(vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md", note)
    with pytest.raises(VaultLockedError):
        reanchor_chain_head(day="2026-10-14")
    assert target.read_text(encoding="utf-8") == note


def test_reanchor_refuses_a_witness_for_another_day(vault, ledger_db):
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    note = _witness(stamp="2026-10-13")
    target = _lock(vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md", note)
    with pytest.raises(VaultLockedError):
        reanchor_chain_head(day="2026-10-14")
    assert target.read_text(encoding="utf-8") == note


@pytest.mark.parametrize(
    "normalize",
    [
        pytest.param(lambda b: b.replace(b"\n", b"\r\n"), id="crlf"),
        pytest.param(lambda b: b"\xef\xbb\xbf" + b, id="bom"),
        pytest.param(lambda b: b"\xef\xbb\xbf" + b.replace(b"\n", b"\r\n"), id="bom-crlf"),
    ],
)
def test_reanchor_recognizes_its_witness_after_line_ending_or_bom_edits(
    vault, ledger_db, normalize
):
    # A sync or editor that rewrites line endings or adds a BOM leaves the
    # witness locked and still this writer's: same head is unchanged, a later
    # head is appended, and neither refuses.
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    assert reanchor_chain_head(day="2026-10-14")["anchor_action"] == "created"
    edited = normalize(target.read_bytes())
    target.write_bytes(edited)

    assert reanchor_chain_head(day="2026-10-14")["anchor_action"] == "unchanged"
    assert target.read_bytes() == edited

    events.record_event("agent.task.updated", payload={"task_id": 8})
    result = reanchor_chain_head(day="2026-10-14")
    assert result["anchor_action"] == "appended"
    assert target.read_bytes().startswith(edited)
    assert result["head_hash"].encode() in target.read_bytes()


# --- reanchor: an unlocked note it did not write is refused too (#2094) ----


@pytest.mark.parametrize(
    "note",
    [
        pytest.param("my own notes about the ledger\n", id="plain"),
        pytest.param("---\ntype: audit-ledger-head\n---\n\nmy own notes\n", id="type-marker"),
        pytest.param("---\ntitle: scratch\n---\n\n- head_hash: forged\n", id="stray-head-hash"),
    ],
)
def test_reanchor_refuses_an_unlocked_note_it_did_not_write(vault, ledger_db, note):
    # The anchor path is fixed by date, so a foreign note there is neither
    # appended to (that would bless it as a witness) nor diverted around; the
    # run fails loud, exactly as for a foreign locked note, and the note is
    # left byte for byte.
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(note, encoding="utf-8")
    with pytest.raises(ToolError, match="not a witness this re-anchor wrote") as info:
        reanchor_chain_head(day="2026-10-14")
    assert not isinstance(info.value, VaultLockedError)
    assert "Move it aside" in str(info.value)
    assert target.read_text(encoding="utf-8") == note


def test_reanchor_refuses_an_unlocked_witness_for_another_day(vault, ledger_db):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    note = _legacy_witness(stamp="2026-10-13")
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(note, encoding="utf-8")
    with pytest.raises(ToolError, match="not a witness this re-anchor wrote"):
        reanchor_chain_head(day="2026-10-14")
    assert target.read_text(encoding="utf-8") == note


def _legacy_witness(head_hash: str = "earlier", *, stamp: str = "2026-10-14") -> str:
    """The note the first re-anchor (#768, before ``locked: true``) wrote for ``stamp``."""
    return (
        "---\n"
        "type: audit-ledger-head\n"
        f"date: {stamp}\n"
        "---\n\n"
        f"# Audit ledger chain head {stamp}\n\n"
        "- status: valid\n"
        "- verified: true\n"
        "- total_verified: 1\n"
        f"- head_hash: {head_hash}\n"
        "- head_ulid: 01TEST\n"
        "- anchored_at: 2026-09-06T12:00:00+00:00\n"
    )


@pytest.mark.parametrize("make", [_witness, _legacy_witness], ids=["current", "legacy"])
def test_reanchor_refuses_a_witness_that_is_not_valid_utf8(vault, ledger_db, make):
    # A witness whose opening is intact but whose body later carries bytes that
    # are not UTF-8 (a sync or editor mangling it) is a ToolError the CLI
    # reports with exit 2, not a UnicodeDecodeError traceback; it is not touched.
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import events
    from unified_agent.audit import ledger_cli
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    raw = make().encode() + b"- note: \xff\xfe broken\n"
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    with pytest.raises(ToolError, match="not valid UTF-8") as info:
        reanchor_chain_head(day="2026-10-14")
    assert "Move it aside" in str(info.value)
    assert target.read_bytes() == raw
    assert ledger_cli.run_reanchor_cli(["--date", "2026-10-14"]) == 2
    assert target.read_bytes() == raw


def test_reanchor_still_appends_to_a_legacy_unlocked_witness(vault, ledger_db):
    # Witnesses filed before #2091 put `locked: true` in the template carry no
    # lock; they are still this writer's and keep their append-only contract.
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    legacy = _legacy_witness()
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(legacy.replace("\n", "\r\n").encode())
    result = reanchor_chain_head(day="2026-10-14")
    assert result["anchor_action"] == "appended"
    text = target.read_bytes().decode()
    assert text.startswith(legacy.replace("\n", "\r\n"))
    assert result["head_hash"] in text

    assert reanchor_chain_head(day="2026-10-14")["anchor_action"] == "unchanged"


# --- learning: the fingerprint check and write share the note lock ---------


def test_learning_proposal_check_and_write_are_one_critical_section(vault, monkeypatch):
    # Two passes over one completed session race to file the same proposal.
    # With the check outside the lock, both saw no proposal and both wrote; now
    # the second waits for the first and finds its proposal already filed.
    started = session_start(agent="Desktop_Agent1", kind="coding")
    sid = started["session_id"]
    session_end(sid, status="completed", summary="Always claim with task_claim before edits.")
    rel = learning._proposal_relpath(sid)
    (vault / rel).unlink()

    real = learning._existing_fingerprint
    first = threading.Event()
    outcomes: dict[str, dict] = {}
    ctx = contextvars.copy_context()

    def rival():
        outcomes["rival"] = ctx.run(learning.propose_session_learnings, sid)

    worker = threading.Thread(target=rival, daemon=True)

    def racing_check(path):
        fp = real(path)
        if not first.is_set():
            first.set()
            worker.start()
            # A rival not blocked on the note lock finishes its whole write now.
            worker.join(timeout=0.5)
        return fp

    monkeypatch.setattr(learning, "_existing_fingerprint", racing_check)
    outcomes["first"] = learning.propose_session_learnings(sid)
    worker.join(timeout=30)
    assert not worker.is_alive()
    written = sorted(name for name, out in outcomes.items() if out["written"])
    existing = sorted(name for name, out in outcomes.items() if out.get("existing"))
    assert written == ["first"], outcomes
    assert existing == ["rival"], outcomes


def test_learning_locked_note_holding_the_proposal_reports_existing(vault):
    # The human locked the filed proposal; re-proposing the same candidates is
    # "already filed", not a lock error.
    started = session_start(agent="Desktop_Agent1", kind="coding")
    sid = started["session_id"]
    session_end(sid, status="completed", summary="Always claim with task_claim before edits.")
    note = vault / learning._proposal_relpath(sid)
    note.write_text(note.read_text(encoding="utf-8").replace("---\n", "---\nlocked: true\n", 1))
    before = note.read_bytes()
    result = learning.propose_session_learnings(sid)
    assert result["written"] is False
    assert result["existing"] is True
    assert note.read_bytes() == before


# --- export: the name is taken by a no-clobber publish --------------------


def test_export_never_overwrites_a_name_claimed_after_the_check(vault, monkeypatch):
    # A rival export lands on the chosen name right after it is resolved (the
    # window the old `while p.exists()` bump left before its write_bytes). The
    # export publishes with a no-clobber hard link (#2094), so the rival's file
    # is never overwritten, the export takes the next free name, and it lands
    # intact with no temp file left beside it.
    monkeypatch.setattr(config, "vault_root", lambda: vault)
    exports = vault / "00 - Inbox" / "exports"
    rivals: list[Path] = []

    def rival_claims(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:
            f.write(b"RIVAL")
        rivals.append(path)

    real_resolve = export_tools.resolve_in_vault
    hooks = {"resolve": False}

    def resolve_then_rival(*a, **k):
        p = real_resolve(*a, **k)
        if not hooks["resolve"]:
            hooks["resolve"] = True
            rival_claims(p)
        return p

    monkeypatch.setattr(export_tools, "resolve_in_vault", resolve_then_rival)
    out = export_tools._deliver("tasks", "csv", "id\n1\n", 1, True)
    written = (vault / out["path"]).resolve()
    assert written.read_bytes() == b"id\n1\n"
    assert rivals and written not in rivals
    assert [r.read_bytes() for r in rivals] == [b"RIVAL"] * len(rivals)
    assert len(list(exports.iterdir())) == len(rivals) + 1


def test_export_same_second_names_do_not_collide(vault, monkeypatch):
    monkeypatch.setattr(config, "vault_root", lambda: vault)
    first = export_tools._deliver("tasks", "csv", "a\n", 1, True)
    second = export_tools._deliver("tasks", "csv", "b\n", 1, True)
    assert first["path"] != second["path"]
    assert (vault / first["path"]).read_bytes() == b"a\n"
    assert (vault / second["path"]).read_bytes() == b"b\n"
