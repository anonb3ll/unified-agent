"""Memory semantic search upkeep: embedding notes that have no vector yet.

Moved out of ``storage`` (task #1720) so the storage layer never imports the
fastembed client. The schema (``memory_embeddings``, the optional
``memory_vec`` index) and the ``cosine_similarity()`` SQL function stay in
core; generating vectors is a service on top of them. ``post_init`` registers
:func:`backfill_memory_embeddings` as a background post-init hook, so it runs
after every ``init_db()`` in the hub, as it did when it lived in storage.
"""

from __future__ import annotations

import sqlite3
import time

from . import config, storage

# Notes embedded per write transaction. The model runs with no transaction
# open; each batch is then written and committed on its own.
BACKFILL_COMMIT_EVERY = 50


def backfill_memory_embeddings() -> None:
    """Embed legacy notes that predate the embeddings table, then mirror any
    memory_embeddings row not yet in memory_vec (covers both freshly-embedded
    notes here and a pre-existing DB upgrading onto a sqlite-vec-enabled
    version for the first time). Best-effort throughout: notes it can't embed
    stay searchable via FTS, and a memory_vec mirroring failure never affects
    memory_embeddings (the source of truth)."""
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT id, title, content FROM memory "
                "WHERE id NOT IN (SELECT note_id FROM memory_embeddings) "
                "AND id NOT LIKE 'masterindex%' "
                "AND id NOT LIKE 'tagindex_%'"
            ).fetchall()
            if rows:
                from .embeddings import (
                    embedding_to_bytes,
                    generate_embedding,
                    is_model_load_failure,
                )

                pause = config.backfill_batch_pause_s()
                model_unavailable = False
                for start in range(0, len(rows), BACKFILL_COMMIT_EVERY):
                    if model_unavailable:
                        break
                    if start and pause:
                        # Yield between batches so request handling keeps up.
                        time.sleep(pause)
                    batch = rows[start : start + BACKFILL_COMMIT_EVERY]
                    # Embed with no transaction open, then write and commit the
                    # batch: other writers wait only for the inserts, and a
                    # restart keeps every batch already committed.
                    vectors = []
                    for row in batch:
                        try:
                            text = f"{row['title'] or ''}\n\n{row['content']}"
                            vectors.append((row, embedding_to_bytes(generate_embedding(text))))
                        except Exception as exc:
                            # The model cannot be loaded: every later row would
                            # retry the load and fail the same way (task #1850).
                            if is_model_load_failure(exc):
                                model_unavailable = True
                                break
                    for row, emb_bytes in vectors:
                        # Store only if the note still holds the text embedded
                        # above; an edit in between leaves it for the next pass.
                        try:
                            stored = conn.execute(
                                "INSERT OR REPLACE INTO memory_embeddings "
                                "(note_id, embedding, model) "
                                "SELECT id, ?, ? FROM memory WHERE id = ? AND title IS ? "
                                "AND content IS ?",
                                (
                                    emb_bytes,
                                    config.embed_model(),
                                    row["id"],
                                    row["title"],
                                    row["content"],
                                ),
                            )
                            if stored.rowcount:
                                storage.upsert_memory_vec(conn, row["id"], emb_bytes)
                        except Exception:
                            pass
                    conn.commit()

            # Mirror pass: any note already in memory_embeddings but missing
            # from memory_vec — a pre-existing DB's full backlog the first
            # time it runs against a sqlite-vec-enabled version.
            mirror_missing_vectors(conn)
        finally:
            conn.close()
    except Exception:
        pass


def mirror_missing_vectors(conn: sqlite3.Connection) -> int:
    """Copy embeddings missing from ``memory_vec`` into it, in batches; return the count.

    Set-based (task #1850): one query reads the mirror's rowids once and
    returns the notes it lacks, instead of a correlated vec0 lookup per
    ``memory_embeddings`` row. Only vectors of the mirror's width count, the
    same rule as ``storage.embedding_coverage``; a wider or narrower model is
    served by the per-row scan and would only fail the insert on every pass.
    The vectors are then read and written ``BACKFILL_COMMIT_EVERY`` at a time
    on the caller's connection, with a commit and a short pause between
    batches. A no-op when sqlite-vec is not loaded on ``conn``.
    """
    try:
        missing = [
            r[0]
            for r in conn.execute(
                "SELECT m.rowid FROM memory_embeddings me JOIN memory m ON m.id = me.note_id "
                "WHERE LENGTH(me.embedding) = ? "
                "AND m.rowid NOT IN (SELECT rowid FROM memory_vec) ORDER BY m.rowid",
                (storage.MEMORY_VEC_DIM * 4,),
            )
        ]
    except sqlite3.OperationalError:
        return 0  # memory_vec unavailable on this connection — nothing to mirror
    pause = config.backfill_batch_pause_s()
    mirrored = 0
    for start in range(0, len(missing), BACKFILL_COMMIT_EVERY):
        if start and pause:
            time.sleep(pause)
        batch = missing[start : start + BACKFILL_COMMIT_EVERY]
        marks = ",".join("?" * len(batch))
        rows = conn.execute(
            "SELECT m.rowid AS rid, me.embedding FROM memory m "
            f"JOIN memory_embeddings me ON me.note_id = m.id WHERE m.rowid IN ({marks})",
            batch,
        ).fetchall()
        for row in rows:
            try:
                # Known absent from the mirror, so a plain INSERT; one that a
                # concurrent writer added meanwhile fails here and is skipped.
                conn.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row["rid"], row["embedding"]),
                )
                mirrored += 1
            except sqlite3.Error:
                pass
        conn.commit()
    return mirrored
