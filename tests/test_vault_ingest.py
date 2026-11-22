"""Tests for vault_to_memory_ingest tool."""

from unified_agent import config
from unified_agent.tools import vault_ingest
from unified_agent.tools.vault_ingest import vault_to_memory_ingest


def test_vault_to_memory_ingest_basic(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    wiki_dir = vault_dir / "Wiki"
    wiki_dir.mkdir(parents=True)

    note_path = wiki_dir / "TestNote.md"
    note_path.write_text(
        "# Test Note\nThis is test vault content for memory search.", encoding="utf-8"
    )

    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    monkeypatch.setattr(
        vault_ingest,
        "vault_list",
        lambda path: {"entries": [{"name": "TestNote.md", "type": "file"}]},
    )
    monkeypatch.setattr(
        vault_ingest, "vault_read", lambda path: {"content": "# Test Note\nThis is test content."}
    )

    saved = []
    monkeypatch.setattr(
        vault_ingest,
        "memory_save",
        lambda content, title, tags, author, overwrite: saved.append((title, content)),
    )

    res = vault_to_memory_ingest(subdirs=["Wiki"])
    assert res["ingested_count"] == 1
    assert len(saved) == 1
    assert saved[0][0] == "TestNote"
