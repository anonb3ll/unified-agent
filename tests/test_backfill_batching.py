"""Embedding backfills commit in batches and embed outside a write transaction.

Both start-up backfills used to hold one write transaction across every model
call in the pass. Any other writer then waited up to busy_timeout (and gave up
after it), and a restart mid-pass threw away every vector computed so far.
These tests watch the store from a second connection while the backfill runs:
the writer lock must be free during each model call, and earlier batches must
already be committed when later ones are being embedded.
"""

from __future__ import annotations

import sqlite3

import pytest

from unified_agent import embeddings, memory_semantic, storage
from unified_agent.tools import indexer

_ROWS = 120


class _Probe:
    """A fake embedder that inspects the store from its own connection."""

    def __init__(self, count_sql: str, on_call=None):
        self.count_sql = count_sql
        self.on_call = on_call
        self.visible: list[int] = []
        self.lock_free: list[bool] = []

    def __call__(self, text: str) -> list[float]:
        conn = storage.connect()
        try:
            conn.execute("PRAGMA busy_timeout=0")
            self.visible.append(conn.execute(self.count_sql).fetchone()[0])
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                self.lock_free.append(True)
            except sqlite3.OperationalError:
                self.lock_free.append(False)
            if self.on_call is not None:
                self.on_call(conn, text)
        finally:
            conn.close()
        return [0.1] * storage.MEMORY_VEC_DIM


def _seed_notes(n: int) -> None:
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO memory (id, title, content, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
            [(f"note-{i:04d}", f"Note {i}", f"body {i}") for i in range(n)],
        )
        conn.execute("DELETE FROM memory_embeddings")
        conn.commit()
    finally:
        conn.close()


def _seed_symbols(n: int) -> None:
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO workspace_symbols (workspace, file_path, symbol_name, symbol_type, "
            "signature, docstring, line_no) VALUES ('ws', 'mod.py', ?, 'function', '()', '', ?)",
            [(f"fn_{i}", i + 1) for i in range(n)],
        )
        conn.commit()
    finally:
        conn.close()


def _count(sql: str) -> int:
    conn = storage.connect()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_memory_backfill_commits_in_batches_without_holding_the_writer_lock(monkeypatch):
    _seed_notes(_ROWS)
    probe = _Probe("SELECT COUNT(*) FROM memory_embeddings")
    monkeypatch.setattr(embeddings, "generate_embedding", probe)

    memory_semantic.backfill_memory_embeddings()

    assert len(probe.visible) == _ROWS
    assert all(probe.lock_free), "writer lock held while the model ran"
    assert max(probe.visible) >= memory_semantic.BACKFILL_COMMIT_EVERY
    assert _count("SELECT COUNT(*) FROM memory_embeddings") == _ROWS


def test_memory_backfill_skips_a_note_edited_while_it_was_embedded(monkeypatch):
    _seed_notes(3)

    def _edit_while_embedding(conn: sqlite3.Connection, text: str) -> None:
        if text.startswith("Note 1\n"):
            conn.execute("UPDATE memory SET content='edited' WHERE id='note-0001'")
            conn.commit()

    probe = _Probe("SELECT COUNT(*) FROM memory_embeddings", _edit_while_embedding)
    monkeypatch.setattr(embeddings, "generate_embedding", probe)

    memory_semantic.backfill_memory_embeddings()

    conn = storage.connect()
    try:
        ids = {r[0] for r in conn.execute("SELECT note_id FROM memory_embeddings")}
    finally:
        conn.close()
    assert ids == {"note-0000", "note-0002"}


def test_symbol_backfill_commits_in_batches_without_holding_the_writer_lock(monkeypatch):
    _seed_symbols(_ROWS)
    probe = _Probe("SELECT COUNT(*) FROM workspace_symbol_embeddings")
    monkeypatch.setattr(embeddings, "generate_embedding", probe)

    indexer._backfill_symbol_embeddings()

    assert len(probe.visible) == _ROWS
    assert all(probe.lock_free), "writer lock held while the model ran"
    assert max(probe.visible) >= indexer.SYMBOL_BACKFILL_COMMIT_EVERY
    assert _count("SELECT COUNT(*) FROM workspace_symbol_embeddings") == _ROWS


@pytest.mark.parametrize("batch", [1, 7])
def test_symbol_backfill_tolerates_any_batch_size(monkeypatch, batch):
    _seed_symbols(10)
    monkeypatch.setattr(indexer, "SYMBOL_BACKFILL_COMMIT_EVERY", batch)
    monkeypatch.setattr(
        embeddings, "generate_embedding", lambda text: [0.1] * storage.MEMORY_VEC_DIM
    )

    indexer._backfill_symbol_embeddings()

    assert _count("SELECT COUNT(*) FROM workspace_symbol_embeddings") == 10
