"""Tests for the structured export tools (task 63 A).

Exports land under 00 - Inbox/exports/ inside the vault write allowlist, or
come back inline. The inline path is capped at WRITE_MAX_BYTES (2 MB); a
direct vault write gets the larger EXPORT_VAULT_MAX_BYTES ceiling. CSV must
be RFC-4180 so it opens in a spreadsheet.
"""

import csv
import errno
import io
import json
import os
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, edition, storage
from unified_agent.tools.export import (
    compare_migration_manifests,
    export_data,
    export_journal,
    export_memory,
    export_migration_manifest,
    export_tasks,
    import_migration_manifest,
    rollback_migration_import,
)
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add, task_update
from unified_agent.tools.vault import vault_journal


def _vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    for d in ("Logs", "00 - Inbox"):
        (v / d).mkdir(parents=True)
    monkeypatch.setattr(config, "vault_root", lambda: v)
    monkeypatch.setattr(config, "vault_write_dirs", lambda: ["00 - Inbox", "Logs", "Wiki"])
    return v


def _write_raw_journal(v, subdir: str, day: str, agent: str, text: str, tags=None) -> None:
    """Write a journal note directly under Logs/<subdir>, bypassing vault_journal.

    Simulates a note left behind by another namespace/host — used to exercise
    export_journal's generic per-subdirectory scan without depending on any
    device-specific routing in vault_journal itself.
    """
    p = v / "Logs" / subdir / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    tag_suffix = "".join(f" #{t}" for t in (tags or []))
    p.write_text(
        f"---\ndate: {day}\ntype: journal\n---\n\n# {day} — Agent journal\n"
        f"- **09:00** [{agent}] {text}{tag_suffix}\n"
    )


class TestExportTasks:
    def test_export_data_dispatches_read_only_export(self, isolated_env):
        task_add("compatibility export")
        out = export_data("tasks", {"format": "json", "write_to_vault": False})
        assert out["rows"] == 1
        assert "compatibility export" in out["content"]

    def test_export_data_rejects_bad_action_and_params(self, isolated_env):
        with pytest.raises(ToolError, match="action must be one of"):
            export_data("unknown")
        with pytest.raises(ToolError, match="invalid params"):
            export_data("tasks", {"not_a_parameter": True})

    def test_csv_is_rfc4180_quoted(self, isolated_env):
        task_add("has, comma", notes='he said "hi"\nline2', priority="high")
        out = export_tasks(format="csv", write_to_vault=False)
        parsed = list(csv.reader(io.StringIO(out["content"])))
        assert parsed[0][0] == "id"  # header
        row = parsed[1]
        assert "has, comma" in row  # comma survived quoting
        assert 'he said "hi"\nline2' in row  # embedded quote+newline survived
        assert out["rows"] == 1

    def test_status_filter_and_json(self, isolated_env):
        a = task_add("open one")
        b = task_add("done one")
        task_update(b["id"], status="done", actor="test-agent")
        out = export_tasks(format="json", status="done", write_to_vault=False)
        recs = json.loads(out["content"])
        assert [r["id"] for r in recs] == [b["id"]]
        assert out["rows"] == 1
        del a

    def test_markdown_escapes_pipes(self, isolated_env):
        task_add("a | b")
        out = export_tasks(format="markdown", write_to_vault=False)
        assert r"a \| b" in out["content"]

    def test_write_to_vault_lands_in_exports_dir(self, tmp_path, monkeypatch, isolated_env):
        _vault(tmp_path, monkeypatch)
        task_add("t1")
        out = export_tasks(format="csv", write_to_vault=True)
        assert out["path"].startswith("00 - Inbox/exports/tasks-")
        assert out["path"].endswith(".csv")
        assert (tmp_path / "vault" / out["path"]).exists()
        assert out["bytes"] > 0

    def test_bad_format_and_status(self, isolated_env):
        with pytest.raises(ToolError, match="format must be"):
            export_tasks(format="xml", write_to_vault=False)
        with pytest.raises(ToolError, match="status must be"):
            export_tasks(status="nope", write_to_vault=False)

    @pytest.mark.parametrize("org_id", ["org_a", None])
    @pytest.mark.parametrize("format", ["csv", "json", "markdown"])
    @pytest.mark.parametrize("write_to_vault", [False, True])
    def test_task_exports_are_tenant_scoped(
        self, isolated_env, tmp_path, monkeypatch, org_id, format, write_to_vault
    ):
        vault = _vault(tmp_path, monkeypatch)
        for owner in ("org_a", "org_b", None):
            monkeypatch.setattr(edition, "tenant_org_id", lambda owner=owner: owner)
            task_add(f"scope-{owner}", project="unified-agent")
        monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
        out = export_data("tasks", {"format": format, "write_to_vault": write_to_vault})
        content = (vault / out["path"]).read_text() if write_to_vault else out["content"]
        assert out["rows"] == 1
        assert f"scope-{org_id}" in content
        for other in {"org_a", "org_b", None} - {org_id}:
            assert f"scope-{other}" not in content

    def test_tenant_fence_composes_with_status_and_project(self, isolated_env, monkeypatch):
        monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
        task_add("foreign", project="unified-agent")
        monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
        expected = task_add("matching", project="unified-agent")
        other_status = task_add("done", project="unified-agent")
        task_update(other_status["id"], status="done", actor="test-agent")
        task_add("other-project", project="browser")
        out = export_tasks(
            format="json", status="open", project="unified-agent", write_to_vault=False
        )
        assert [row["id"] for row in json.loads(out["content"])] == [expected["id"]]


class TestExportMemory:
    def test_excludes_index_notes(self, isolated_env):
        memory_save("a real note", title="Real", tags=["t"], author="A")
        out = export_memory(format="jsonl", write_to_vault=False)
        recs = [json.loads(line) for line in out["content"].splitlines()]
        assert all(not r["id"].startswith("tagindex_") for r in recs)
        assert all(r["id"] != "masterindex00" for r in recs)
        assert any(r["title"] == "Real" for r in recs)

    def test_tag_filter(self, isolated_env):
        memory_save("keep me", title="K", tags=["keep"], author="A")
        memory_save("drop me", title="D", tags=["other"], author="A")
        out = export_memory(format="json", tag="keep", write_to_vault=False)
        recs = json.loads(out["content"])
        assert [r["title"] for r in recs] == ["K"]

    def test_since_bad_date_raises(self, isolated_env):
        with pytest.raises(ToolError, match="ISO date"):
            export_memory(since="not-a-date", write_to_vault=False)

    def test_source_host_included(self, monkeypatch, isolated_env):
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-host")
        memory_save("a real note", title="Real", tags=["t"], author="A")
        out = export_memory(format="json", write_to_vault=False)
        recs = json.loads(out["content"])
        assert len(recs) == 1
        assert recs[0]["source_host"] == "desktop-host"

    def test_vault_write_allows_over_inline_cap(self, tmp_path, monkeypatch, isolated_env):
        """A payload between WRITE_MAX_BYTES and EXPORT_VAULT_MAX_BYTES is
        rejected inline but written to the vault (the weekly-export path)."""
        _vault(tmp_path, monkeypatch)
        monkeypatch.setattr(config, "WRITE_MAX_BYTES", 2_000)
        monkeypatch.setattr(config, "EXPORT_VAULT_MAX_BYTES", 5_000_000)
        for i in range(40):
            memory_save(f"note {i} " + "x" * 80, title=f"N{i}", tags=["t"], author="A")

        with pytest.raises(ToolError, match="over the 2000 byte cap"):
            export_memory(format="json", write_to_vault=False)

        out = export_memory(format="json", write_to_vault=True)
        assert out["bytes"] > 2_000
        assert out["path"].startswith("00 - Inbox/exports/memory-")


class TestMigrationManifest:
    def test_manifest_is_deterministic_and_preserves_provenance(self, monkeypatch, isolated_env):
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-host")
        task = task_add("manifest task", notes="[waiting-for: review]")
        task_update(task["id"], archived=True)
        memory_save("manifest note", title="Manifest", tags=["migration"], author="A")

        first = export_migration_manifest(write_to_vault=False)
        second = export_migration_manifest(write_to_vault=False)

        assert first["manifest"]["fingerprint"] == second["manifest"]["fingerprint"]
        row = next(row for row in first["manifest"]["tasks"] if row["id"] == task["id"])
        assert row["archived_at"] is not None
        assert row["source_host"] == "desktop-host"
        assert first["manifest"]["source_host"] == "desktop-host"

    def test_private_manifest_withholds_foreign_and_unowned_events(self, isolated_env, monkeypatch):
        monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
        private = task_add("private owner")
        monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
        foreign = task_add("foreign owner")
        for kind, ref, summary in [
            ("task", str(private["id"]), "private event"),
            ("task", str(foreign["id"]), "foreign secret event"),
            ("task", None, "unowned secret event"),
            ("task", "999999", "missing owner secret event"),
            ("journal", str(private["id"]), "unattributed kind secret event"),
        ]:
            storage.record_agent_event(kind, ref_id=ref, agent="Desktop_Agent2", summary=summary)
        monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
        manifest = export_migration_manifest()["manifest"]
        assert [row["id"] for row in manifest["tasks"]] == [private["id"]]
        assert {row["ref_id"] for row in manifest["agent_events"]} == {str(private["id"])}
        assert any(row["summary"] == "private event" for row in manifest["agent_events"])
        assert "secret event" not in json.dumps(manifest["agent_events"])
        with storage.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]
        assert manifest["agent_events_withheld"] == total - len(manifest["agent_events"]) == 5

    @pytest.mark.parametrize("org_id", ["org_a", None])
    def test_manifest_task_rows_are_tenant_scoped(self, isolated_env, monkeypatch, org_id):
        owned = {}
        for owner in ("org_a", "org_b", None):
            monkeypatch.setattr(edition, "tenant_org_id", lambda owner=owner: owner)
            owned[owner] = task_add(f"manifest-{owner}")["id"]
        monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
        manifest = export_migration_manifest()["manifest"]
        assert [row["id"] for row in manifest["tasks"]] == [owned[org_id]]

    @pytest.mark.parametrize("org_id", ["org_a", None])
    def test_manifest_withholds_oversized_integer_refs(self, isolated_env, monkeypatch, org_id):
        monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
        task = task_add("valid event owner")
        oversized = [str(1 << 63), str(-(1 << 63) - 1), "9" * 100]
        for ref in oversized:
            storage.record_agent_event(
                "task", ref_id=ref, agent="Desktop_Agent2", summary="invalid"
            )
        before = export_migration_manifest()["manifest"]
        storage.record_agent_event(
            "task", ref_id=str(task["id"]), agent="Desktop_Agent2", summary="valid"
        )
        after = export_migration_manifest()["manifest"]
        assert after["agent_events"][-1]["summary"] == "valid"
        assert len(after["agent_events"]) == len(before["agent_events"]) + 1
        assert after["agent_events_withheld"] == before["agent_events_withheld"] == len(oversized)
        assert not set(oversized) & {row["ref_id"] for row in after["agent_events"]}

    def test_compare_classifies_claim_conflict_and_unmatched_rows(self):
        left = {
            "manifest_version": 1,
            "source_host": "desktop",
            "fingerprint": "left",
            "tasks": [
                {"id": 1, "title": "same", "notes": "", "claimed_by": "Desktop_Agent1"},
                {"id": 2, "title": "desktop only", "notes": "", "claimed_by": None},
            ],
            "memory": [],
        }
        right = {
            "manifest_version": 1,
            "source_host": "laptop",
            "fingerprint": "right",
            "tasks": [
                {"id": 1, "title": "same changed", "notes": "", "claimed_by": "Laptop_Agent1"},
                {"id": 3, "title": "laptop only", "notes": "", "claimed_by": None},
            ],
            "memory": [],
        }

        diff = compare_migration_manifests(left, right)
        types = {conflict["type"] for conflict in diff["conflicts"]}
        assert "active_claim_conflict" in types
        assert "left_only" in types
        assert "right_only" in types
        assert diff["requires_human_disposition"] is True


class TestMigrationImport:
    """Gate C: reconciliation/import of an export_migration_manifest() snapshot.

    Each test moves UA_HOME mid-test to simulate a second, disjoint database —
    the same shape a real cross-host import has, without spinning up a second
    process.
    """

    def _switch_home(self, tmp_path, monkeypatch, name):
        from unified_agent import storage

        monkeypatch.setenv("UA_HOME", str(tmp_path / name))
        storage.init_db()

    def test_import_preserves_ids_relationships_and_history(
        self, tmp_path, monkeypatch, isolated_env
    ):
        parent = task_add("parent task")
        child = task_add("child task", parent_id=parent["id"])
        task_update(child["id"], notes="claimed-by: Desktop_Agent1", status="doing")
        note = memory_save("migration note", title="Note", tags=["x"], author="A")
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        assert export_tasks(write_to_vault=False)["rows"] == 0  # disjoint target, sanity check

        result = import_migration_manifest(manifest, apply=True)

        assert result["applied"] is True
        assert set(result["tasks"]["inserted"]) == {parent["id"], child["id"]}
        assert result["tasks"]["held"] == []
        assert result["memory"]["inserted"] == [note["id"]]
        assert result["memory"]["held"] == []

        target_tasks = {
            row["id"]: row
            for row in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
        }
        source_tasks = {row["id"]: row for row in manifest["tasks"]}
        assert target_tasks[parent["id"]] == source_tasks[parent["id"]]
        assert target_tasks[child["id"]] == source_tasks[child["id"]]
        assert target_tasks[child["id"]]["parent_id"] == parent["id"]
        assert target_tasks[child["id"]]["status"] == "doing"
        assert "claimed-by: Desktop_Agent1" in target_tasks[child["id"]]["notes"]

        target_memory = {
            row["id"]: row
            for row in export_migration_manifest(write_to_vault=False)["manifest"]["memory"]
        }
        source_memory = {row["id"]: row for row in manifest["memory"]}
        assert target_memory[note["id"]] == source_memory[note["id"]]

    def test_import_resolves_parent_even_when_child_listed_first(
        self, tmp_path, monkeypatch, isolated_env
    ):
        """A manifest whose task list is not id-ordered (hand-edited, or from
        a future exporter that sorts differently) must not treat an
        unresolved parent as ready just because its id was already seen.
        Reproduces a real bug found on review: the first resolution pass
        pre-populated a lookup cache with an entry for every pending id
        (including still-unresolved ones), and a later `in` check on that
        cache tested key presence rather than resolution — so a child
        processed in the same pass as its not-yet-inserted parent could
        pass the readiness check before the parent actually existed,
        producing an INSERT that violates the parent_id foreign key."""
        parent = task_add("parent task")
        child = task_add("child task", parent_id=parent["id"])
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]
        # Reverse the list so the child is resolved before its parent.
        manifest["tasks"] = list(reversed(manifest["tasks"]))
        assert manifest["tasks"][0]["id"] == child["id"]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        result = import_migration_manifest(manifest, apply=True)

        assert result["tasks"]["held"] == []
        assert set(result["tasks"]["inserted"]) == {parent["id"], child["id"]}
        target_tasks = {
            row["id"]: row
            for row in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
        }
        assert target_tasks[child["id"]]["parent_id"] == parent["id"]

    def test_unresolvable_parent_is_held_not_inserted(self, tmp_path, monkeypatch, isolated_env):
        parent = task_add("parent task")
        child = task_add("child task", parent_id=parent["id"])
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]
        # Drop the parent row entirely — the child's parent_id now points at
        # nothing this import can ever place.
        manifest["tasks"] = [t for t in manifest["tasks"] if t["id"] != parent["id"]]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        result = import_migration_manifest(manifest, apply=True)

        assert result["tasks"]["inserted"] == []
        assert result["tasks"]["held"] == [{"id": child["id"], "reason": "missing_parent"}]
        assert export_tasks(write_to_vault=False)["rows"] == 0  # nothing written at all

    def test_reapplying_same_manifest_writes_nothing(self, tmp_path, monkeypatch, isolated_env):
        task = task_add("solo task")
        memory_save("solo note", title="Solo", tags=[])
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        first = import_migration_manifest(manifest, apply=True)
        assert task["id"] in first["tasks"]["inserted"]
        before = export_migration_manifest(write_to_vault=False)["manifest"]

        second = import_migration_manifest(manifest, apply=True)
        after = export_migration_manifest(write_to_vault=False)["manifest"]

        assert second["tasks"]["inserted"] == []
        assert second["memory"]["inserted"] == []
        assert task["id"] in second["tasks"]["unchanged"]
        assert second["tasks"]["held"] == []
        assert second["memory"]["held"] == []
        # No mutation at all: the two post-import snapshots are byte-identical.
        assert before["fingerprint"] == after["fingerprint"]

    def test_conflicting_local_row_is_held_not_overwritten(
        self, tmp_path, monkeypatch, isolated_env
    ):
        task = task_add("original title")
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]
        # Mutate the source's copy of the same id so it now disagrees with the
        # (still-open, unclaimed) row already sitting in the target below.
        manifest["tasks"][0]["title"] = "title changed upstream"
        manifest["tasks"][0]["notes"] = "claimed-by: Laptop_Agent1"

        self._switch_home(tmp_path, monkeypatch, "target_home")
        local = task_add("original title")
        assert local["id"] == task["id"]  # autoincrement in a fresh db starts at 1 too
        task_update(local["id"], notes="claimed-by: Desktop_Agent1")

        before = export_migration_manifest(write_to_vault=False)["manifest"]
        before_row = next(r for r in before["tasks"] if r["id"] == task["id"])

        result = import_migration_manifest(manifest, apply=True)

        assert result["tasks"]["inserted"] == []
        assert [h["id"] for h in result["tasks"]["held"]] == [task["id"]]
        assert result["tasks"]["held"][0]["reason"] == "conflicting_edit"

        after = export_migration_manifest(write_to_vault=False)["manifest"]
        after_row = next(r for r in after["tasks"] if r["id"] == task["id"])
        assert after_row == before_row  # local row is untouched, byte-for-byte
        assert after_row["notes"] == "claimed-by: Desktop_Agent1"

    def test_rejects_non_manifest_input(self, isolated_env):
        with pytest.raises(ToolError, match="manifest_version"):
            import_migration_manifest({"not": "a manifest"}, apply=True)

    def test_dry_run_previews_without_writing(self, tmp_path, monkeypatch, isolated_env):
        task_add("preview me")
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        preview = import_migration_manifest(manifest, apply=False)
        assert preview["applied"] is False
        assert "rollback_token" not in preview
        assert preview["tasks"]["inserted"] != []
        # Nothing was actually written.
        assert export_tasks(write_to_vault=False)["rows"] == 0

    def test_rollback_restores_exact_pre_import_state(self, tmp_path, monkeypatch, isolated_env):
        parent = task_add("parent")
        task_add("child", parent_id=parent["id"])
        memory_save("note", title="N", tags=[])
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        pre_import_snapshot = export_migration_manifest(write_to_vault=False)["manifest"]
        assert pre_import_snapshot["tasks"] == []
        assert pre_import_snapshot["memory"] == []

        applied = import_migration_manifest(manifest, apply=True)
        assert applied["rollback_token"]["tasks"]
        mid_state = export_migration_manifest(write_to_vault=False)["manifest"]
        assert mid_state["tasks"] != []

        rollback_migration_import(applied["rollback_token"])
        post_rollback = export_migration_manifest(write_to_vault=False)["manifest"]

        assert post_rollback["tasks"] == pre_import_snapshot["tasks"]
        assert post_rollback["memory"] == pre_import_snapshot["memory"]

    def test_rollback_only_touches_its_own_inserted_rows(self, tmp_path, monkeypatch, isolated_env):
        imported = task_add("from manifest")
        manifest = export_migration_manifest(write_to_vault=False)["manifest"]
        # Only the one task this test cares about — an id collision against
        # unrelated manifest rows is a separate, already-covered scenario
        # (test_conflicting_local_row_is_held_not_overwritten).
        manifest["tasks"] = [t for t in manifest["tasks"] if t["id"] == imported["id"]]

        self._switch_home(tmp_path, monkeypatch, "target_home")
        applied = import_migration_manifest(manifest, apply=True)
        assert applied["tasks"]["held"] == []
        assert applied["rollback_token"]["tasks"] == [imported["id"]]

        # Created only after the import, so its id can't collide with the
        # imported row (two independently-autoincremented DBs both start at
        # 1 — a same-id collision is the separate scenario covered by
        # test_conflicting_local_row_is_held_not_overwritten).
        native = task_add("native to target, unrelated")
        assert native["id"] != imported["id"]

        rollback_migration_import(applied["rollback_token"])

        remaining = {
            row["id"]
            for row in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
        }
        assert remaining == {native["id"]}


class TestExportJournal:
    def test_parses_and_filters_by_agent(self, tmp_path, monkeypatch, isolated_env):
        _vault(tmp_path, monkeypatch)
        vault_journal("did a thing", agent="Desktop_Agent1", tags=["x"])
        vault_journal("other agent", agent="Desktop_agent3")
        out = export_journal(agent="Desktop_Agent1", write_to_vault=False)
        recs = [json.loads(line) for line in out["content"].splitlines()]
        assert len(recs) == 1
        assert recs[0]["agent"] == "Desktop_Agent1"
        assert recs[0]["text"] == "did a thing"
        assert recs[0]["tags"] == ["x"]

    def test_empty_when_no_logs(self, tmp_path, monkeypatch, isolated_env):
        _vault(tmp_path, monkeypatch)
        out = export_journal(write_to_vault=False)
        assert out["rows"] == 0
        assert out["content"] == ""

    def test_exports_subdirectory_namespace_entries(self, tmp_path, monkeypatch, isolated_env):
        """A root-only scan must not silently drop entries from a Logs subdirectory."""
        v = _vault(tmp_path, monkeypatch)
        _write_raw_journal(v, "Laptop", "2026-08-08", "Laptop_Agent1", "laptop did a thing", ["y"])

        out = export_journal(write_to_vault=False)
        recs = [json.loads(line) for line in out["content"].splitlines()]
        assert [r["text"] for r in recs] == ["laptop did a thing"]
        assert recs[0]["machine"] == "laptop"
        assert recs[0]["agent"] == "Laptop_Agent1"
        assert recs[0]["tags"] == ["y"]

    def test_merges_desktop_and_subdirectory_namespaces(self, tmp_path, monkeypatch, isolated_env):
        v = _vault(tmp_path, monkeypatch)
        vault_journal("desktop entry", agent="Desktop_Agent1")
        _write_raw_journal(v, "Laptop", "2026-08-08", "Laptop_Agent1", "laptop entry")

        out = export_journal(write_to_vault=False)
        recs = [json.loads(line) for line in out["content"].splitlines()]
        assert {r["machine"] for r in recs} == {"desktop", "laptop"}
        assert {r["text"] for r in recs} == {"desktop entry", "laptop entry"}

        # `agent` filtering still applies across both namespaces.
        only = export_journal(agent="Laptop_Agent1", write_to_vault=False)
        recs = [json.loads(line) for line in only["content"].splitlines()]
        assert [r["text"] for r in recs] == ["laptop entry"]


@pytest.mark.parametrize("value", [-(1 << 63), (1 << 63) - 1])
def test_event_integer_refs_accept_sqlite_boundary(value):
    from unified_agent.tools.export import _native_key

    assert _native_key(str(value), "INTEGER") == value


#: Every distinct errno that means "no hard links here", by the platform's own
#: constants (Linux aliases ENOTSUP to EOPNOTSUPP; Darwin does not).
_LINK_UNSUPPORTED_PARAMS = [
    pytest.param(code, id=name)
    for code, name in {
        getattr(errno, n): n for n in ("EXDEV", "ENOSYS", "EOPNOTSUPP", "ENOTSUP", "EPERM")
    }.items()
]


class TestDeliverNeverLeavesAPartialFile:
    """A vault export lands whole or not at all (#2094).

    The bytes go to a private hidden temp file in the export directory and are
    fsynced; only then is the temp published with a no-clobber ``os.link`` into
    the first free export name (the next ``-N`` suffix on ``FileExistsError``).
    No export name is ever claimed empty and later replaced or unlinked, so a
    failure removes only the temp, and a note another writer puts at an export
    name keeps its bytes. Without hard links the export falls back to an
    exclusive create written in place, which reports (and never deletes) a
    possibly incomplete file or a name taken over mid-write.
    """

    DATA = "id,title\n" + "".join(f"{i},row {i}\n" for i in range(200))

    @staticmethod
    def _exports(v):
        d = v / "00 - Inbox" / "exports"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def test_normal_path_writes_whole_file_and_fsyncs(self, tmp_path, monkeypatch, isolated_env):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        synced: list[int] = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(export_tools.os, "fsync", spy_fsync)
        out = export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        written = v / out["path"]
        assert written.read_bytes() == self.DATA.encode()
        assert out["bytes"] == len(self.DATA.encode())
        assert synced, "the export bytes are fsynced before they take the claimed name"
        # Only the export itself: no temp file or placeholder left beside it.
        assert self._exports(v) == [written.name]

    def test_mid_write_failure_leaves_no_file(self, tmp_path, monkeypatch, isolated_env):
        import builtins

        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        exports = (v / "00 - Inbox" / "exports").resolve()
        real_open = builtins.open

        class _HalfWriter:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                self._f.write(data[: len(data) // 2])
                self._f.flush()
                raise OSError(errno.ENOSPC, "No space left on device")

            def __getattr__(self, name):
                return getattr(self._f, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._f.close()
                return False

        def failing_open(file, mode="r", *a, **k):
            f = real_open(file, mode, *a, **k)
            if "b" in mode and any(c in mode for c in "wxa"):
                if os.path.realpath(os.path.dirname(file)) == str(exports):
                    return _HalfWriter(f)
            return f

        monkeypatch.setattr(export_tools, "open", failing_open, raising=False)
        with pytest.raises(ToolError, match="No space left on device"):
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert self._exports(v) == []

    def test_publish_failure_leaves_no_file(self, tmp_path, monkeypatch, isolated_env):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)

        def failing_link(src, dst, *a, **k):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(export_tools.os, "link", failing_link)
        with pytest.raises(ToolError, match="Input/output error"):
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert self._exports(v) == []

    # --- a foreign note at the export name is never overwritten or deleted --
    # Obsidian Sync or an operator can atomically put a (locked) note at the
    # name an export is about to use while the export is still writing. An
    # exclusive create owns an inode only at the instant it runs, so the export
    # must not publish an empty claim and later replace or unlink whatever sits
    # at that name (Desktop_Agent2 review of #1222, P2).

    FOREIGN = b"---\nlocked: true\n---\noperator-authored note\n"
    STAMP = "20261014T120000Z"

    def _freeze_and_intercept(self, v, monkeypatch, export_tools):
        """Freeze the export timestamp and swap a foreign note in at the first export name
        the moment the export writes its bytes. Returns that name."""
        import builtins
        import datetime as real_dt
        import types

        class _Fixed(real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=real_dt.UTC)

        monkeypatch.setattr(
            export_tools,
            "datetime",
            types.SimpleNamespace(datetime=_Fixed, UTC=real_dt.UTC, date=real_dt.date),
        )
        exports = v / "00 - Inbox" / "exports"
        final = exports / f"tasks-{self.STAMP}.csv"
        real_open = builtins.open
        foreign = self.FOREIGN
        swapped: list[bool] = []

        class _SwapOnWrite:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                if not swapped:
                    swapped.append(True)
                    replacement = v / "replacement.md"
                    replacement.write_bytes(foreign)
                    os.replace(replacement, final)
                return self._f.write(data)

            def __getattr__(self, name):
                return getattr(self._f, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._f.__exit__(*exc)

        def intercepting_open(file, mode="r", *a, **k):
            f = real_open(file, mode, *a, **k)
            if "b" in mode and any(c in mode for c in "wxa"):
                if os.path.realpath(os.path.dirname(file)) == os.path.realpath(exports):
                    return _SwapOnWrite(f)
            return f

        monkeypatch.setattr(export_tools, "open", intercepting_open, raising=False)
        return final, swapped

    def test_foreign_note_at_the_export_name_survives_a_successful_export(
        self, tmp_path, monkeypatch, isolated_env
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        final, swapped = self._freeze_and_intercept(v, monkeypatch, export_tools)
        out = export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert swapped, "the foreign note was swapped in mid-export"
        written = (v / out["path"]).resolve()
        assert written != final.resolve()
        assert written.read_bytes() == self.DATA.encode()
        assert final.read_bytes() == self.FOREIGN
        assert self._exports(v) == sorted([final.name, written.name])

    @pytest.mark.parametrize("failure", ["fsync", "publish"])
    def test_foreign_note_at_the_export_name_survives_a_failed_export(
        self, tmp_path, monkeypatch, isolated_env, failure
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        final, swapped = self._freeze_and_intercept(v, monkeypatch, export_tools)

        def fail(*a, **k):
            raise OSError(errno.EIO, "Input/output error")

        if failure == "fsync":
            monkeypatch.setattr(export_tools.os, "fsync", fail)
        else:
            monkeypatch.setattr(export_tools.os, "link", fail)
        with pytest.raises(ToolError, match="Input/output error"):
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert swapped
        assert final.read_bytes() == self.FOREIGN
        assert self._exports(v) == [final.name]

    # --- cleanup is best effort, independent, and never hides the error -----

    def test_temp_cleanup_failure_keeps_the_original_error_and_names_the_leftover(
        self, tmp_path, monkeypatch, isolated_env
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_unlink = os.unlink

        def failing_fsync(fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        def failing_unlink(path, *a, **k):
            if str(path).endswith(".tmp"):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(path, *a, **k)

        monkeypatch.setattr(export_tools.os, "fsync", failing_fsync)
        monkeypatch.setattr(export_tools.os, "unlink", failing_unlink)
        with pytest.raises(ToolError, match="No space left on device") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        monkeypatch.setattr(export_tools.os, "unlink", real_unlink)
        msg = str(info.value)
        leftovers = self._exports(v)
        # Only the private temp survives, and the error names it rather than
        # promising nothing was left.
        assert len(leftovers) == 1 and leftovers[0].endswith(".tmp")
        assert leftovers[0] in msg
        assert "nothing was left" not in msg

    def test_temp_cleanup_failure_after_publish_still_returns_the_export(
        self, tmp_path, monkeypatch, isolated_env
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_unlink = os.unlink

        def failing_unlink(path, *a, **k):
            if str(path).endswith(".tmp"):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(path, *a, **k)

        monkeypatch.setattr(export_tools.os, "unlink", failing_unlink)
        out = export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        monkeypatch.setattr(export_tools.os, "unlink", real_unlink)
        assert (v / out["path"]).read_bytes() == self.DATA.encode()
        leftover = out["leftover_temp"]
        assert leftover.endswith(".tmp") and (v / leftover).is_file()
        assert sorted(self._exports(v)) == sorted([Path(out["path"]).name, Path(leftover).name])

    # --- no hard links: the main-branch in-place exclusive write -----------

    @pytest.mark.parametrize("err", _LINK_UNSUPPORTED_PARAMS)
    def test_without_hard_links_falls_back_to_an_exclusive_in_place_write(
        self, tmp_path, monkeypatch, isolated_env, err
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)

        def no_links(src, dst, *a, **k):
            raise OSError(err, "links not supported")

        monkeypatch.setattr(export_tools.os, "link", no_links)
        first = export_tools._deliver("tasks", "csv", "a\n", 1, True)
        second = export_tools._deliver("tasks", "csv", "b\n", 1, True)
        assert first["path"] != second["path"]
        assert (v / first["path"]).read_bytes() == b"a\n"
        assert (v / second["path"]).read_bytes() == b"b\n"
        assert sorted(self._exports(v)) == sorted(
            [Path(first["path"]).name, Path(second["path"]).name]
        )

    def test_fallback_write_failure_says_the_file_may_be_incomplete(
        self, tmp_path, monkeypatch, isolated_env
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_fsync = os.fsync
        calls: list[int] = []

        def no_links(src, dst, *a, **k):
            raise OSError(errno.EPERM, "links not supported")

        def second_fsync_fails(fd):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)

        monkeypatch.setattr(export_tools.os, "link", no_links)
        monkeypatch.setattr(export_tools.os, "fsync", second_fsync_fails)
        with pytest.raises(ToolError, match="No space left on device") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert "may be incomplete" in str(info.value)
        # The in-place file is not deleted: its name cannot be proven ours.
        leftovers = self._exports(v)
        assert len(leftovers) == 1 and not leftovers[0].endswith(".tmp")
        assert leftovers[0] in str(info.value)

    def test_a_failed_export_frees_its_name_for_the_next(self, tmp_path, monkeypatch, isolated_env):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)

        def failing_fsync(fd):
            raise OSError(errno.EIO, "Input/output error")

        with monkeypatch.context() as m:
            m.setattr(export_tools.os, "fsync", failing_fsync)
            with pytest.raises(ToolError):
                export_tools._deliver("tasks", "csv", "a\n", 1, True)
        assert self._exports(v) == []
        out = export_tools._deliver("tasks", "csv", "b\n", 1, True)
        assert (v / out["path"]).read_bytes() == b"b\n"
        assert self._exports(v) == [(v / out["path"]).name]

    # --- review round 2 of #1222 ------------------------------------------

    @staticmethod
    def _no_links(err=errno.EPERM):
        def no_links(src, dst, *a, **k):
            raise OSError(err, "links not supported")

        return no_links

    def test_a_link_permission_error_is_reported_not_a_fallback(
        self, tmp_path, monkeypatch, isolated_env
    ):
        # EACCES from link(2) is a real permission refusal on the directory,
        # not a filesystem without hard links: surface it, write nothing.
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        monkeypatch.setattr(export_tools.os, "link", self._no_links(errno.EACCES))
        with pytest.raises(ToolError, match="EACCES"):
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert self._exports(v) == []

    def test_fallback_reports_a_name_taken_over_mid_write(
        self, tmp_path, monkeypatch, isolated_env
    ):
        # Devin's repro: without hard links the export writes in place; a note
        # renamed onto that name mid-write leaves the descriptor on the
        # displaced inode. The export must not report success at a path that
        # now holds someone else's note, and must not touch that note.
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        exports = v / "00 - Inbox" / "exports"
        real_fsync = os.fsync
        calls: list[int] = []

        def fsync_then_steal(fd):
            calls.append(fd)
            real_fsync(fd)
            if len(calls) == 2:  # the in-place descriptor (the first is the temp)
                (final,) = [p for p in exports.iterdir() if not p.name.startswith(".")]
                stolen = v / "replacement.md"
                stolen.write_bytes(self.FOREIGN)
                os.replace(stolen, final)

        monkeypatch.setattr(export_tools.os, "link", self._no_links())
        monkeypatch.setattr(export_tools.os, "fsync", fsync_then_steal)
        with pytest.raises(ToolError, match="taken over") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        (final,) = [p for p in exports.iterdir() if not p.name.startswith(".")]
        assert final.read_bytes() == self.FOREIGN
        assert final.name in str(info.value)
        assert "not delivered" in str(info.value)
        assert self._exports(v) == [final.name]

    def test_fallback_create_error_is_a_tool_error(self, tmp_path, monkeypatch, isolated_env):
        import builtins

        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_open = builtins.open

        def refuse_final_create(file, mode="r", *a, **k):
            if mode == "xb" and not os.path.basename(file).startswith("."):
                raise PermissionError(errno.EACCES, "Permission denied", str(file))
            return real_open(file, mode, *a, **k)

        monkeypatch.setattr(export_tools.os, "link", self._no_links())
        monkeypatch.setattr(export_tools, "open", refuse_final_create, raising=False)
        with pytest.raises(ToolError, match="EACCES") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert "00 - Inbox/exports/tasks-" in str(info.value)
        assert self._exports(v) == []

    def test_fallback_failure_with_temp_cleanup_failure_names_both(
        self, tmp_path, monkeypatch, isolated_env
    ):
        # Codex r2: link EPERM, the in-place fsync ENOSPC, the temp unlink EIO.
        # The original error survives and the surviving temp is named.
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_fsync, real_unlink = os.fsync, os.unlink
        calls: list[int] = []

        def second_fsync_fails(fd):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)

        def temp_unlink_fails(path, *a, **k):
            if str(path).endswith(".tmp"):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(path, *a, **k)

        monkeypatch.setattr(export_tools.os, "link", self._no_links())
        monkeypatch.setattr(export_tools.os, "fsync", second_fsync_fails)
        monkeypatch.setattr(export_tools.os, "unlink", temp_unlink_fails)
        with pytest.raises(ToolError, match="No space left on device") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        monkeypatch.setattr(export_tools.os, "unlink", real_unlink)
        msg = str(info.value)
        names = self._exports(v)
        (tmp,) = [n for n in names if n.endswith(".tmp")]
        (final,) = [n for n in names if not n.endswith(".tmp")]
        assert tmp in msg and final in msg
        assert "may be incomplete" in msg and "ENOSPC" in msg

    def test_name_exhaustion_with_temp_cleanup_failure_names_the_temp(
        self, tmp_path, monkeypatch, isolated_env
    ):
        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_unlink = os.unlink

        def always_taken(src, dst, *a, **k):
            raise FileExistsError(errno.EEXIST, "File exists", str(dst))

        def temp_unlink_fails(path, *a, **k):
            if str(path).endswith(".tmp"):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(path, *a, **k)

        monkeypatch.setattr(export_tools, "_EXPORT_NAME_ATTEMPTS", 3)
        monkeypatch.setattr(export_tools.os, "link", always_taken)
        monkeypatch.setattr(export_tools.os, "unlink", temp_unlink_fails)
        with pytest.raises(ToolError, match="could not claim a free export name") as info:
            export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        monkeypatch.setattr(export_tools.os, "unlink", real_unlink)
        (tmp,) = self._exports(v)
        assert tmp.endswith(".tmp") and tmp in str(info.value)

    def test_the_export_directory_is_fsynced_after_publish(
        self, tmp_path, monkeypatch, isolated_env
    ):
        import stat

        from unified_agent.tools import export as export_tools

        _vault(tmp_path, monkeypatch)
        real_fsync, real_link = os.fsync, os.link
        events: list[str] = []

        def spy_link(src, dst, *a, **k):
            events.append("link")
            return real_link(src, dst, *a, **k)

        def spy_fsync(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append(f"fsync-{kind}")
            return real_fsync(fd)

        monkeypatch.setattr(export_tools.os, "link", spy_link)
        monkeypatch.setattr(export_tools.os, "fsync", spy_fsync)
        export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert events[0] == "fsync-file"
        assert "fsync-dir" in events
        assert events.index("link") < len(events) - 1 - events[::-1].index("fsync-dir")

    def test_a_directory_fsync_failure_does_not_fail_or_undo_the_export(
        self, tmp_path, monkeypatch, isolated_env
    ):
        import stat

        from unified_agent.tools import export as export_tools

        v = _vault(tmp_path, monkeypatch)
        real_fsync = os.fsync

        def dir_fsync_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_fsync(fd)

        monkeypatch.setattr(export_tools.os, "fsync", dir_fsync_fails)
        out = export_tools._deliver("tasks", "csv", self.DATA, 200, True)
        assert (v / out["path"]).read_bytes() == self.DATA.encode()
        assert self._exports(v) == [Path(out["path"]).name]
