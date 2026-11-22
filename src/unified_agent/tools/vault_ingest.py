"""Vault-to-Memory Ingestion Pipeline tool.

Scans Obsidian vault notes in target directories (Wiki/, 00 - Inbox/) and automatically
indexes/embeds their content into SQLite memory.db for hybrid FTS5 and semantic search.
"""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from .memory import memory_save
from .vault import vault_list, vault_read


def vault_to_memory_ingest(
    subdirs: list[str] | None = None,
    author: str = "Desktop_agent3",
) -> dict[str, Any]:
    """Ingest and index human vault notes (Wiki/, 00 - Inbox/) into SQLite memory.db.

    Enables fast hybrid FTS5 full-text and semantic vector search across vault notes.

    Args:
        subdirs: Vault subdirectories to ingest (default ["Wiki", "00 - Inbox"]).
        author: Agent label to attribute ingested memory notes to (default 'Desktop_agent3').

    Returns:
        {
          "ingested_count": int,
          "skipped_count": int,
          "notes_processed": list[str]
        }
    """
    if subdirs is None:
        subdirs = ["Wiki", "00 - Inbox"]

    root = config.vault_root()
    if not root or not root.exists():
        raise ToolError("Vault root directory not configured or does not exist")

    ingested = 0
    skipped = 0
    processed = []

    for s_dir in subdirs:
        try:
            listing = vault_list(path=s_dir)
            entries = listing.get("entries", [])
        except Exception:
            continue

        for entry in entries:
            if entry["type"] != "file" or not entry["name"].endswith(".md"):
                continue

            rel_path = f"{s_dir}/{entry['name']}"
            try:
                res = vault_read(path=rel_path)
                content = res.get("content", "").strip()
            except Exception:
                skipped += 1
                continue

            if not content:
                skipped += 1
                continue

            title = entry["name"][:-3]
            tags = ["vault", f"vault:{s_dir.lower().replace(' ', '_')}"]

            # Save to SQLite memory DB
            try:
                memory_save(
                    content=content,
                    title=title,
                    tags=tags,
                    author=author,
                    overwrite=True,
                )
                ingested += 1
                processed.append(rel_path)
            except Exception:
                skipped += 1

    return {
        "ingested_count": ingested,
        "skipped_count": skipped,
        "notes_processed": processed,
    }
