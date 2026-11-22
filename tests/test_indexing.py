import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools.memory import (
    MASTER_INDEX_ID,
    _tag_index_id,
    memory_delete,
    memory_get,
    memory_list,
    memory_save,
    memory_search,
)


class TestIndexing:
    def test_indexing_on_save(self):
        # 1. Save an untagged note
        note_untagged = memory_save("Content of untagged note", title="Untagged Note")

        # Check that Master Index is created and can be fetched directly by ID
        master = memory_get(MASTER_INDEX_ID)
        assert "Memory Master Index" in master["title"]
        assert "untagged" in master["content"]
        assert _tag_index_id("untagged") in master["content"]

        # Check tag-specific index for untagged notes
        tag_idx = memory_get(_tag_index_id("untagged"))
        assert "Memory Index: untagged" in tag_idx["title"]
        assert "Untagged Note" in tag_idx["content"]
        assert note_untagged["id"] in tag_idx["content"]
        assert "Content of untagged note" in tag_idx["content"]

        # 2. Save a tagged note
        note_tagged = memory_save(
            "Acme project details", title="Acme Project", tags=["project:acme"]
        )

        master = memory_get(MASTER_INDEX_ID)
        assert "project:acme" in master["content"]
        assert _tag_index_id("project:acme") in master["content"]

        acme_idx = memory_get(_tag_index_id("project:acme"))
        assert "Acme Project" in acme_idx["content"]
        assert note_tagged["id"] in acme_idx["content"]

    def test_indexing_on_update(self):
        note = memory_save(
            "Initial content description", title="Original Title", tags=["project:test"]
        )

        # Verify initial summary in index
        idx = memory_get(_tag_index_id("project:test"))
        assert "Initial content description" in idx["content"]

        # Update note content and title
        memory_save(
            "New updated description text",
            title="Updated Title",
            tags=["project:test"],
            id=note["id"],
        )

        idx = memory_get(_tag_index_id("project:test"))
        assert "Updated Title" in idx["content"]
        assert "New updated description text" in idx["content"]
        assert "Initial content description" not in idx["content"]

    def test_update_retag_refreshes_old_tag_index(self):
        note = memory_save("Retagged content here", title="Mover", tags=["project:a"])
        assert "Mover" in memory_get(_tag_index_id("project:a"))["content"]

        # Move the note from project:a to project:b
        memory_save("Retagged content here", title="Mover", tags=["project:b"], id=note["id"])

        # Old tag has no notes left: its index note must be gone
        with pytest.raises(ToolError, match="no memory note with id"):
            memory_get(_tag_index_id("project:a"))
        assert "Mover" in memory_get(_tag_index_id("project:b"))["content"]
        master = memory_get(MASTER_INDEX_ID)
        assert "project:b" in master["content"]
        assert "project:a" not in master["content"]

    def test_indexing_on_delete_and_cleanup(self):
        note1 = memory_save("Note one content", title="Note One", tags=["project:del"])
        note2 = memory_save("Note two content", title="Note Two", tags=["project:del"])

        idx = memory_get(_tag_index_id("project:del"))
        assert "Note One" in idx["content"]
        assert "Note Two" in idx["content"]

        # Delete note1. Notes saved without an owner are removable by a human.
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            memory_delete(note1["id"])
        finally:
            config.agent_identity_var.reset(token)
        idx = memory_get(_tag_index_id("project:del"))
        assert "Note One" not in idx["content"]
        assert "Note Two" in idx["content"]

        # Delete note2 (which leaves no notes for tag project:del)
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            memory_delete(note2["id"])
        finally:
            config.agent_identity_var.reset(token)

        # Tag index note for project:del should be completely deleted from database
        with pytest.raises(ToolError, match="no memory note with id"):
            memory_get(_tag_index_id("project:del"))

        # Master index should no longer contain project:del tag
        master = memory_get(MASTER_INDEX_ID)
        assert "project:del" not in master["content"]

    def test_search_and_list_excludes_indexes_by_default(self):
        memory_save("A unique piece of search text", title="Target Note", tags=["project:unique"])

        # Searching for the unique text should find the target note
        res = memory_search("unique piece")["results"]
        assert len(res) == 1
        assert res[0]["title"] == "Target Note"

        # Searching for metadata should not return the index notes by default
        res_meta = memory_search("Memory Index")["results"]
        assert len(res_meta) == 0

        # Listing notes should exclude index notes by default
        items = memory_list()["items"]
        for item in items:
            assert not any(t.startswith("meta:index") for t in item["tags"])

        # But we can find them if we explicitly request meta:index tag
        res_idx = memory_search("Index", tags=["meta:index"])["results"]
        assert len(res_idx) > 0
        assert any(r["id"] == MASTER_INDEX_ID for r in res_idx)

    def test_tag_index_ids_do_not_collide(self):
        # Distinct tags that sanitize identically must map to distinct index notes
        assert _tag_index_id("project:acme") != _tag_index_id("project acme")
        memory_save("Colon tagged", title="Colon", tags=["project:acme"])
        memory_save("Space tagged", title="Space", tags=["project acme"])
        assert "Colon" in memory_get(_tag_index_id("project:acme"))["content"]
        assert "Space" in memory_get(_tag_index_id("project acme"))["content"]

    def test_user_meta_prefixed_tag_is_still_indexed(self):
        # A user tag that merely starts with "meta:index" is a normal note tag:
        # it gets cataloged, and the note stays visible in default listing.
        note = memory_save("Note about indexing itself", title="Meta Note", tags=["meta:indexing"])
        assert "Meta Note" in memory_get(_tag_index_id("meta:indexing"))["content"]
        assert any(i["id"] == note["id"] for i in memory_list(tag="meta:indexing")["items"])
