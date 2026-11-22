"""Overlapping embedding chunks for long memory notes (task #2182).

The fastembed model reads 512 tokens, about 2,000 characters of the notes this
store holds. A longer note's single vector describes only its opening, so a
fact later in the note is invisible to the semantic leg. This module stores
extra vectors for that tail and scores the note by the best one.

Short notes stay on the single ``memory_embeddings`` / ``memory_vec`` path.
Chunking is gated by ``UA_MEMORY_CHUNK_EMBEDDINGS`` (see
``config.memory_chunk_embeddings``). The table is created from ``_migrate``;
rows for notes that predate it are filled by :func:`backfill_memory_chunks`.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import config, memory_knn, storage

# bge-small-en-v1.5 truncates at 512 tokens. The LongMemEval scorecard measured
# that window at about 2,000 characters, so one token is about four characters
# of the notes we store. There is no cheap tokenizer on the save path; the
# model would have to load just to count tokens. Characters are the cut.
CHARS_PER_TOKEN = 4
CHUNK_TOKENS = 400
OVERLAP_TOKENS = 80
CHUNK_CHARS = CHUNK_TOKENS * CHARS_PER_TOKEN  # 1,600: inside the 512-token window
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN  # 320
# A note the model can read in one pass keeps the single vector only.
SINGLE_VECTOR_MAX_CHARS = 512 * CHARS_PER_TOKEN  # 2,048
# Sixteen windows cover about 20,000 characters (stride 1,280). The tail past
# that is not embedded. The cap is also what keeps one note from dominating
# the semantic scan.
MAX_CHUNKS = 16
# Prefer a break on whitespace this close to the cut, so a word is not split.
SNAP_CHARS = 80

# Notes embedded per backfill transaction. Each note is several vectors, so
# the batch is smaller than the single-vector backfill.
BACKFILL_BATCH = 8


def embed_source(title: str | None, content: str) -> str:
    """The text the single-vector path embeds, so a chunk is a slice of it."""
    return f"{title or ''}\n\n{content}"


def needs_chunks(title: str | None, content: str) -> bool:
    return len(embed_source(title, content)) > SINGLE_VECTOR_MAX_CHARS


def chunk_text(text: str) -> list[str]:
    """Overlapping windows, or ``[]`` when one vector already covers ``text``.

    Each window is about 400 tokens. The next starts about 80 tokens before
    the cut. A cut prefers the last newline or space in its final
    ``SNAP_CHARS`` so a word is not split. At most ``MAX_CHUNKS`` windows;
    anything past the last window is not embedded.
    """
    if len(text) <= SINGLE_VECTOR_MAX_CHARS:
        return []
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < MAX_CHUNKS:
        end = min(start + CHUNK_CHARS, length)
        if end < length:
            window = text[start:end]
            floor = max(0, len(window) - SNAP_CHARS)
            snap = window.rfind("\n", floor)
            if snap < 0:
                snap = window.rfind(" ", floor)
            # Keep the window from collapsing to a sliver on a snap at the start.
            if snap >= CHUNK_CHARS // 2:
                end = start + snap
        if end <= start:
            end = min(start + CHUNK_CHARS, length)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        nxt = end - OVERLAP_CHARS
        if nxt <= start:
            nxt = end
        start = nxt
    return chunks


def embed_chunk_vectors(title: str | None, content: str) -> list[bytes] | None:
    """Vectors for a long note, or ``None`` when chunking does not apply.

    ``None`` means "do not store chunks" (flag off, or the note fits in one
    vector). A model failure propagates so the caller can leave the note for
    the backfill. The vectors are computed with no database lock held.
    """
    if not config.memory_chunk_embeddings():
        return None
    pieces = chunk_text(embed_source(title, content))
    if not pieces:
        return None
    from .embeddings import embedding_to_bytes, generate_embeddings

    return [embedding_to_bytes(vector) for vector in generate_embeddings(pieces)]


def write_chunks(conn: sqlite3.Connection, note_id: str, vectors: list[bytes]) -> int:
    """Replace a note's chunks with ``vectors``. The caller owns the transaction.

    Org id is copied from the note, so the fence trigger accepts the row.
    The vec0 mirror is best-effort: a missing extension leaves the blob rows,
    which the cosine scan still reads. Returns how many chunk rows were written.
    """
    storage.delete_memory_chunks(conn, note_id)
    if not vectors:
        return 0
    model = config.embed_model()
    written = 0
    mirror = True
    for index, embedding in enumerate(vectors[:MAX_CHUNKS]):
        cur = conn.execute(
            "INSERT INTO memory_chunks (note_id, chunk_index, embedding, model, org_id) "
            "SELECT ?, ?, ?, ?, org_id FROM memory WHERE id = ?",
            (note_id, index, embedding, model, note_id),
        )
        if cur.rowcount != 1:
            break
        written += 1
        if not mirror:
            continue
        try:
            conn.execute(
                "INSERT INTO memory_chunk_vec (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, embedding),
            )
        except sqlite3.Error:
            # The mirror is optional. Stop trying it for the rest of this note;
            # the blob rows above remain searchable through the UDF scan.
            mirror = False
    return written


def max_similarities(
    conn: sqlite3.Connection,
    emb_bytes: bytes,
    *,
    where_sql: str,
    where_params: tuple,
    min_similarity: float | None = None,
) -> dict[str, float]:
    """Best cosine similarity per note over its chunks.

    ``min_similarity`` (task #2214) lets the vec0 path stop once every unseen
    chunk is below the floor; a note whose best chunk is under it may then be
    missing. Without it the cut must exhaust the mirror, and a mirror past
    sqlite-vec's k limit falls back to the UDF scan.

    ``where_sql`` is the same predicate ``_semantic_similarity_rows`` applies
    to ``memory`` (tenant, index notes, superseded, deleted), including its
    leading ``AND``. An empty dict means no chunk contributed: the caller
    keeps the single-vector score. The flag is checked by that caller.

    The vec0 mirror is used only when it holds every compatible chunk blob.
    A chunk row with no mirror row falls back to the cosine UDF over
    ``memory_chunks``, so that window is scored instead of waiting for
    :func:`mirror_missing_chunk_vectors`. An empty mirror uses the same UDF.
    A query the mirror cannot hold skips it.
    """
    if not storage._has_sqlite_table(conn, "memory_chunks"):
        return {}
    model_sql, model_params = storage.compatible_embedding_model_clause("c.model")
    scores: dict[str, float] = {}
    mirror_holds_query = len(emb_bytes) == storage.MEMORY_VEC_DIM * 4
    if mirror_holds_query and storage._has_sqlite_table(conn, "memory_chunk_vec"):
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM memory_chunk_vec").fetchone()["n"]
            # A lagging mirror would otherwise hide the blob rows the KNN
            # cannot see. Fall back to the UDF rather than return a partial cut.
            lagging = conn.execute(
                "SELECT 1 FROM memory_chunks c "
                "WHERE length(c.embedding) = ? "
                "AND c.rowid NOT IN (SELECT rowid FROM memory_chunk_vec) "
                "LIMIT 1",
                (storage.MEMORY_VEC_DIM * 4,),
            ).fetchone()
            if total and lagging is None:
                # The join predicates run after each KNN cut; bounded_knn grows
                # k until the cut is proven (task #2214), as for memory_vec.

                def resolve(raw):
                    sims = dict(raw)
                    found = conn.execute(
                        "SELECT c.rowid AS rid, m.id AS note_id "
                        "FROM memory_chunks c JOIN memory m ON m.id = c.note_id "
                        "WHERE c.rowid IN (SELECT value FROM json_each(?)) "
                        f"AND {model_sql} {where_sql}",
                        (json.dumps(list(sims)), *model_params, *where_params),
                    ).fetchall()
                    return [{"note_id": r["note_id"], "similarity": sims[r["rid"]]} for r in found]

                rows = memory_knn.bounded_knn(
                    conn,
                    "memory_chunk_vec",
                    emb_bytes,
                    resolve=resolve,
                    min_similarity=min_similarity,
                )
                if rows is not None:
                    for row in rows:
                        note_id = row["note_id"]
                        similarity = row["similarity"]
                        if note_id not in scores or similarity > scores[note_id]:
                            scores[note_id] = similarity
                    return scores
        except sqlite3.OperationalError:
            pass
    rows = conn.execute(
        "SELECT m.id AS note_id, cosine_similarity(c.embedding, ?) AS similarity "
        "FROM memory_chunks c JOIN memory m ON m.id = c.note_id "
        f"WHERE {model_sql} {where_sql}",
        (emb_bytes, *model_params, *where_params),
    ).fetchall()
    for row in rows:
        note_id = row["note_id"]
        similarity = row["similarity"]
        if similarity is None:
            continue
        if note_id not in scores or similarity > scores[note_id]:
            scores[note_id] = similarity
    return scores


def mirror_missing_chunk_vectors(conn: sqlite3.Connection) -> int:
    """Copy chunk blobs the vec0 mirror lacks. A no-op without sqlite-vec."""
    if not storage._has_sqlite_table(conn, "memory_chunk_vec"):
        return 0
    try:
        missing = [
            row[0]
            for row in conn.execute(
                "SELECT c.rowid FROM memory_chunks c "
                "WHERE length(c.embedding) = ? "
                "AND c.rowid NOT IN (SELECT rowid FROM memory_chunk_vec) "
                "ORDER BY c.rowid",
                (storage.MEMORY_VEC_DIM * 4,),
            )
        ]
    except sqlite3.OperationalError:
        return 0
    mirrored = 0
    batch_size = 50
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        marks = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT rowid, embedding FROM memory_chunks WHERE rowid IN ({marks})",
            batch,
        ).fetchall()
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO memory_chunk_vec (rowid, embedding) VALUES (?, ?)",
                    (row["rowid"], row["embedding"]),
                )
                mirrored += 1
            except sqlite3.Error:
                pass
        conn.commit()
    return mirrored


def backfill_memory_chunks() -> None:
    """Embed long live notes that have no chunks yet, in bounded batches.

    Idempotent: a note that already has a chunk row is skipped, and a note
    whose text changed between the read and the write is left for the next
    pass. Best-effort throughout, matching ``backfill_memory_embeddings``.
    Does nothing while chunking is off, so a host that has not opted in does
    not spend a startup embedding the corpus.
    """
    if not config.memory_chunk_embeddings():
        return
    try:
        conn = storage.connect()
        try:
            from .embeddings import is_model_load_failure

            pause = config.backfill_batch_pause_s()
            cursor = 0
            model_unavailable = False
            started = False
            while not model_unavailable:
                rows = conn.execute(
                    "SELECT rowid, id, title, content FROM memory "
                    "WHERE rowid > ? AND superseded_by IS NULL "
                    "AND instr(tags, '\"meta:deleted\"') = 0 "
                    "AND id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%' "
                    "AND length(content) + length(coalesce(title, '')) > ? "
                    "AND NOT EXISTS (SELECT 1 FROM memory_chunks c WHERE c.note_id = memory.id) "
                    "ORDER BY rowid LIMIT ?",
                    (cursor, SINGLE_VECTOR_MAX_CHARS - 2, BACKFILL_BATCH),
                ).fetchall()
                if not rows:
                    break
                cursor = rows[-1]["rowid"]
                if started and pause:
                    time.sleep(pause)
                started = True
                pending: list[tuple] = []
                for row in rows:
                    if not needs_chunks(row["title"], row["content"]):
                        continue
                    try:
                        vectors = embed_chunk_vectors(row["title"], row["content"])
                    except Exception as exc:
                        if is_model_load_failure(exc):
                            model_unavailable = True
                            break
                        continue
                    if vectors:
                        pending.append((row, vectors))
                for row, vectors in pending:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        current = conn.execute(
                            "SELECT title, content, superseded_by, tags FROM memory WHERE id = ?",
                            (row["id"],),
                        ).fetchone()
                        unchanged = (
                            current is not None
                            and current["title"] == row["title"]
                            and current["content"] == row["content"]
                            and current["superseded_by"] is None
                            and '"meta:deleted"' not in (current["tags"] or "")
                        )
                        if unchanged:
                            write_chunks(conn, row["id"], vectors)
                        conn.commit()
                    except Exception:
                        conn.rollback()
            mirror_missing_chunk_vectors(conn)
        finally:
            conn.close()
    except Exception:
        pass
