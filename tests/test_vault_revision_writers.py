"""Hub-internal vault writers under revision checks (task #2111).

The hub's own read-modify-write writers pass the revision they read, so they
keep working with ``UA_VAULT_REQUIRE_REVISION=1`` and an edit landing between
their read and their write makes them re-read and retry instead of losing it.
"""

import datetime as dt
import hashlib
from datetime import UTC, datetime

import pytest

from unified_agent import delegation_ledger_log, planning_loop
from unified_agent.recall_curation import memory_promote
from unified_agent.tools import vault as vault_mod
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add
from unified_agent.tools.vault import (
    MERGE_WRITE_RETRIES,
    VaultRevisionConflict,
    vault_write,
    vault_write_merged,
)

PLAN = "Wiki/Product-1-Approval-Ledger-Plan.md"
INDEX = "Wiki/Index.md"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "Wiki", "Logs"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    monkeypatch.setenv("UA_VAULT_REQUIRE_REVISION", "1")
    return root


def _edit_after_first_read(monkeypatch, target: str, edit, times: int = 1):
    """Make another writer change ``target`` right after the first ``times`` reads of it."""
    real = vault_mod.note_snapshot
    reads = {"n": 0}

    def snapshot(path):
        result = real(path)
        if path == target and reads["n"] < times:
            reads["n"] += 1
            edit()
        return result

    monkeypatch.setattr(vault_mod, "note_snapshot", snapshot)
    return reads


class TestMergedWrite:
    def test_concurrent_edit_is_merged_not_lost(self, vault, monkeypatch):
        note = vault / "Wiki" / "merge.md"
        note.write_text("a\n")
        reads = _edit_after_first_read(
            monkeypatch, "Wiki/merge.md", lambda: note.write_text(note.read_text() + "other\n")
        )
        vault_write_merged("Wiki/merge.md", lambda current: (current or "") + "mine\n")
        assert reads["n"] == 1
        assert note.read_text() == "a\nother\nmine\n"

    def test_gives_up_after_bounded_retries_without_writing_blind(self, vault, monkeypatch):
        note = vault / "Wiki" / "busy.md"
        note.write_text("0\n")
        counter = {"n": 0}

        def edit():
            counter["n"] += 1
            note.write_text(f"{counter['n']}\n")

        _edit_after_first_read(monkeypatch, "Wiki/busy.md", edit, times=100)
        with pytest.raises(VaultRevisionConflict, match="nothing was overwritten"):
            vault_write_merged("Wiki/busy.md", lambda current: "mine\n")
        assert counter["n"] == MERGE_WRITE_RETRIES + 1
        assert note.read_text() == f"{MERGE_WRITE_RETRIES + 1}\n"

    def test_build_none_skips_the_write(self, vault):
        note = vault / "Wiki" / "same.md"
        note.write_text("same\n")
        assert vault_write_merged("Wiki/same.md", lambda current: None) is None
        assert note.read_text() == "same\n"


class TestMemoryPromote:
    def _note(self):
        return memory_save(
            content="durable promoted rule body",
            title="Durable rule",
            author="Desktop_Agent1",
            confidence=0.9,
        )["id"]

    def test_promote_twice_under_require_revision(self, vault):
        (vault / INDEX).write_text("# Index\n\nsteward prose\n")
        note_id = self._note()
        first = memory_promote(note_id, slug="durable-rule")
        second = memory_promote(note_id, slug="durable-rule")
        assert first["promoted"] and second["promoted"]
        index = (vault / INDEX).read_text()
        assert "steward prose" in index
        assert index.count("[[Wiki/Learnings/durable-rule") == 1
        assert (vault / "Wiki" / "Learnings" / "durable-rule.md").is_file()

    def test_index_edit_between_read_and_write_is_kept(self, vault, monkeypatch):
        index = vault / INDEX
        index.write_text("# Index\n\nsteward prose\n")
        reads = _edit_after_first_read(
            monkeypatch,
            INDEX,
            lambda: index.write_text(index.read_text() + "\nsteward added this\n"),
        )
        result = memory_promote(self._note(), slug="raced-rule")
        assert reads["n"] == 1
        assert result["index_updated"] is True
        text = index.read_text()
        assert "steward added this" in text
        assert "[[Wiki/Learnings/raced-rule" in text

    def test_learning_page_edit_conflicts_without_discarding_it(self, vault, monkeypatch):
        note_id = self._note()
        memory_promote(note_id, slug="raced-page")
        rel = "Wiki/Learnings/raced-page.md"
        note = vault / rel
        original = note.read_text()

        def edit():
            vault_write(rel, "\nother writer context\n", append=True)

        _edit_after_first_read(monkeypatch, rel, edit)
        with pytest.raises(VaultRevisionConflict):
            memory_promote(note_id, slug="raced-page")
        assert note.read_text() == original + "\nother writer context\n"


class TestPlanningLoop:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        monkeypatch.setattr(planning_loop, "_read_tasks", lambda: [])
        monkeypatch.setattr(planning_loop, "_release_tags", lambda _repo: [])
        monkeypatch.setattr(planning_loop, "_release_description", lambda _repo: "")

    def _seed_plan(self, vault, human="human context"):
        (vault / PLAN).write_text(
            "# Plan\n\n## 1. Live status\n"
            f"{planning_loop._PLAN_BEGIN}\nold generated\n{planning_loop._PLAN_END}\n\n"
            f"## 2. Notes\n\n{human}\n"
        )

    def test_rerun_same_day_under_require_revision(self, vault):
        self._seed_plan(vault)
        day = dt.date(2026, 10, 15)
        planning_loop.run_planning_loop(generated_at=day, write=True)
        result = planning_loop.run_planning_loop(generated_at=day, write=True)
        assert result["status"] == "written"
        plan = (vault / PLAN).read_text()
        assert "human context" in plan and "old generated" not in plan
        assert (vault / "Wiki" / "Decision-Packets" / "Decision-Packet-2026-10-15.md").is_file()

    def test_plan_edit_between_read_and_write_is_remerged(self, vault, monkeypatch):
        self._seed_plan(vault)
        plan = vault / PLAN
        reads = _edit_after_first_read(
            monkeypatch,
            PLAN,
            lambda: plan.write_text(plan.read_text().replace("human context", "human edit")),
        )
        result = planning_loop.run_planning_loop(generated_at=dt.date(2026, 10, 15), write=True)
        assert reads["n"] == 1
        text = plan.read_text()
        assert "human edit" in text and "old generated" not in text
        assert result["artifacts"]["plan"] == text

    def test_goal_proposal_rerun_under_require_revision(self, vault, monkeypatch):
        epic = task_add("Epic", notes="[project-spec] [product-1]\nDoD: ship it")
        monkeypatch.setattr(
            planning_loop,
            "_read_tasks",
            lambda: [
                {
                    "id": epic["id"],
                    "title": "Epic",
                    "notes": "[project-spec] [product-1]\nDoD: ship it",
                    "status": "open",
                    "parent_id": None,
                }
            ],
        )
        day = dt.date(2026, 10, 15)
        path, _ = planning_loop._proposal("Grow it", epic["id"], write=True, generated_at=day)
        again, _ = planning_loop._proposal("Grow it", epic["id"], write=True, generated_at=day)
        assert path == again
        assert (vault / path).is_file()

    def test_packet_edit_conflicts_without_discarding_it(self, vault, monkeypatch):
        self._seed_plan(vault)
        day = dt.date(2026, 10, 15)
        planning_loop.run_planning_loop(generated_at=day, write=True)
        rel = "Wiki/Decision-Packets/Decision-Packet-2026-10-15.md"
        packet = vault / rel
        original = packet.read_text()
        _edit_after_first_read(
            monkeypatch, rel, lambda: vault_write(rel, "\noperator context\n", append=True)
        )
        with pytest.raises(VaultRevisionConflict):
            planning_loop.run_planning_loop(generated_at=day, write=True)
        assert packet.read_text() == original + "\noperator context\n"

    @pytest.mark.parametrize("cli", [False, True], ids=["proposal-writer", "cli-proposal"])
    def test_proposal_edit_conflicts_without_discarding_it(self, vault, monkeypatch, cli):
        import sys

        epic = {
            "id": 7,
            "title": "Epic",
            "notes": "[project-spec]\nDoD: ship it",
            "parent_id": None,
        }
        monkeypatch.setattr(planning_loop, "_read_tasks", lambda: [epic])
        day = dt.date(2026, 10, 15)
        rel, _ = planning_loop._proposal("Grow it", 7, write=True, generated_at=day)
        proposal = vault / rel
        original = proposal.read_text()
        _edit_after_first_read(
            monkeypatch, rel, lambda: vault_write(rel, "\noperator context\n", append=True)
        )
        if cli:
            monkeypatch.setattr(planning_loop, "run_planning_loop", lambda **kw: {})
            monkeypatch.setattr(
                sys,
                "argv",
                ["planning-loop", "--goal", "Grow it", "--epic-id", "7", "--date", str(day)],
            )
            with pytest.raises(SystemExit) as exc:
                planning_loop.main()
            assert exc.value.code == 1
        else:
            with pytest.raises(VaultRevisionConflict):
                planning_loop._proposal("Grow it", 7, write=True, generated_at=day)
        assert proposal.read_text() == original + "\noperator context\n"

    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
    @pytest.mark.parametrize("require_revision", ["0", "1"])
    def test_legacy_plan_newlines_merge_with_raw_revision(
        self, vault, monkeypatch, newline, require_revision
    ):
        monkeypatch.setenv("UA_VAULT_REQUIRE_REVISION", require_revision)
        raw = newline.join(
            [
                "# Plan",
                "",
                "## 1. Live status",
                "old generated",
                "",
                "## 2. Notes",
                "human context",
                "",
            ]
        ).encode()
        plan = vault / PLAN
        plan.write_bytes(raw)
        text, revision = vault_mod.note_snapshot(PLAN)
        assert "\r" not in text
        assert revision == "sha256:" + hashlib.sha256(raw).hexdigest()
        result = planning_loop.run_planning_loop(generated_at=dt.date(2026, 10, 15), write=True)
        assert result["status"] == "written"
        assert "human context" in plan.read_text()
        assert "Historical live-status snapshot" in plan.read_text()
        assert planning_loop._PLAN_BEGIN in plan.read_text()
        assert "old generated" in plan.read_text()  # retained as the historical snapshot


class TestDelegationLedger:
    def test_page_created_after_probe_is_appended_not_overwritten(self, vault, monkeypatch):
        tid = task_add("ledger-me")["id"]
        page = "Logs/Delegation Ledger 2026-10.md"
        vault_write(page, "# Delegation Ledger — 2026-10\n\n- earlier line\n")
        # The probe ran before another writer created the page.
        monkeypatch.setattr(delegation_ledger_log, "_page_exists", lambda _page: False)
        out = delegation_ledger_log.append_review_ledger(
            [{"task_id": tid, "verdict": "validated", "notification": "changed"}],
            now=datetime(2026, 10, 15, 8, 0, tzinfo=UTC),
        )
        assert out["created"] is False
        text = (vault / page).read_text()
        assert "- earlier line" in text
        assert f"#{tid} | validated" in text
