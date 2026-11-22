"""Ranked task search (task #1419, Parity 14).

memory_fts is the only fts5 table storage.py has: task lookup was substring-only
(`task_list(notes_contain=...)`). This module adds a second one, `task_fts`,
using the exact rowid-mirroring pattern memory_fts already documents (see
storage.py's `memory_vec` comment) -- an external-content fts5 table keyed to
`tasks.id` via `content_rowid='id'`, kept in sync by AFTER INSERT/UPDATE/DELETE
triggers, with an idempotent backfill for rows that predate the table.

Ranking reuses the *documented* combined-score formula (this repo's own
AGENTS.md, "Memory Search & Ranking" section): semantic + FTS +
recency + salience + access, not the extra entity/feedback/vote terms
memory_search has since grown on top of it (those are memory-note-specific
extensions, not the documented formula this task was asked to reuse). Of the
five documented terms, two do not exist for a task row:

  - salience: a memory-only column (`memory.salience`). Tasks have no
    equivalent "how important is this" field -- `priority` is a workflow
    field (queue ordering), not a relevance judgment, and folding it in would
    be inventing a signal, not reusing one. Fixed at 0.0 and named in
    `explanation.not_applicable` rather than silently dropped or faked.
  - access: a memory-only column (`memory.access_count`, bumped by
    memory_search itself). Tasks are not "recalled" the way notes are, and
    there is no read-access counter on the tasks table. Same treatment: 0.0,
    named explicitly.

So a task's combined_score is semantic + FTS + recency only. See
docs/task-search.md for the full rubric, weights, and worked examples.
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import storage, table_schemas

# The DDL lives in core (table_schemas) so storage.init_db never imports this
# module; the old name stays importable.
_TASK_FTS_SCHEMA = table_schemas.TASK_FTS

# OR-joined terms can match most of the table on a common word, so bound both
# the FTS candidate set (best bm25 first) and how many uncached tasks a single
# query may embed inline.
FTS_CANDIDATE_CAP = 200
LAZY_EMBED_CAP = 32

# Terms the documented formula names that a task row has no equivalent for.
# Kept as a module constant so the search result, the rubric doc, and any
# future reviewer asking "why is this only 3 terms" all point at one place.
NOT_APPLICABLE = (
    "salience (memory-only column; tasks have no relevance-importance field "
    "-- priority is a queue-order field, not reused here to avoid faking a signal)",
    "access (memory-only column; tasks have no read-access counter)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on every startup, cheap when already applied.

    Creates task_fts + its sync triggers (a no-op past the first call, like
    every other `ensure_schema` in this codebase) and backfills task_fts rows
    for any task that predates it -- checked by an anti-join against
    task_fts's own rowid, so a re-run only ever touches genuinely missing
    rows, never rewrites ones already indexed.
    """
    table_schemas.task_search(conn)
    _backfill_task_fts(conn)


def backfill_task_fts(conn: sqlite3.Connection) -> None:
    """Post-init hook: index tasks that predate task_fts (see ``post_init``).

    ``storage.init_db`` creates task_fts and its triggers (the DDL is in
    ``table_schemas``); this fills in rows the triggers never saw.
    """
    _backfill_task_fts(conn)


def _backfill_task_fts(conn: sqlite3.Connection) -> None:
    """Insert FTS rows for any task whose id is missing from the index.

    `task_fts` is an external-content table (content='tasks'): a plain
    `SELECT ... FROM task_fts` with no MATCH clause reads straight through to
    the `tasks` table by rowid rather than reporting the FTS index's own
    membership, so it cannot be used as the anti-join here -- it would report
    every task as "already indexed" even immediately after this function
    deliberately removed one (see the trigger-parity test in
    tests/test_task_search.py). `task_fts_docsize` is fts5's own shadow table,
    one row per rowid actually present in the index, so it is what
    reads-consistently reflects true membership.
    """
    rows = conn.execute(
        "SELECT id, title, notes FROM tasks WHERE id NOT IN (SELECT rowid FROM task_fts_docsize)"
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO task_fts(rowid, title, notes) VALUES (?, ?, ?)",
            (row["id"], row["title"], row["notes"]),
        )


def _get_weight(env_name: str, default: float) -> float:
    try:
        v = float(os.environ.get(env_name, str(default)))
    except ValueError:
        return default
    return v if math.isfinite(v) and v >= 0 else default


def _sanitize_fts_or_query(tokens: list[str]) -> str:
    """OR-join query tokens as fts5 phrase queries.

    memory_search's fts_query joins quoted tokens with an implicit AND
    (space-separated quoted phrases), which is right for a targeted lookup
    but wrong for a ranked, natural-language task search: "who is working on
    the login bug" would need every one of those six words in the same task
    row to match at all, and bm25 never gets a chance to rank partial
    matches. This is the ranked-search-specific reason to differ from
    memory_search here -- memory_search itself is untouched.

    Quoting each token as its own phrase ("token") neutralizes fts5's
    special operators when used unquoted -- AND/OR/NOT/NEAR, ^column,
    *prefix, -exclude, parentheses -- since those only carry syntactic
    meaning outside a quoted phrase; inside one they would just be literal
    characters to match. The one character that still escapes a quoted
    phrase is an embedded double-quote (it closes the phrase early and lets
    whatever follows be interpreted as fts5 syntax again), so that is
    stripped from each token before quoting. A token that is empty after
    stripping (e.g. the caller passed a bare `"`) is dropped rather than
    emitting an empty `""` phrase, which fts5 rejects as a syntax error.
    Returns "" if every token was empty -- the caller treats that as "no FTS
    candidates," not a query error (the query itself was still non-blank).
    """
    parts = []
    for tok in tokens:
        cleaned_tok = tok.replace('"', "").strip()
        if cleaned_tok:
            parts.append(f'"{cleaned_tok}"')
    return " OR ".join(parts)


def _store_task_embedding(
    conn: sqlite3.Connection, task_id: int, title: str, notes: str | None
) -> bool | None:
    """Best-effort, mirrors memory.py's `_store_embedding`: a task must stay
    searchable via FTS even when fastembed can't load."""
    try:
        from .embeddings import embedding_to_bytes, generate_embedding

        emb_bytes = embedding_to_bytes(generate_embedding(f"{title or ''}\n\n{notes or ''}"))
        # Inference runs without a writer lock. Insert atomically only if the
        # task text still matches the snapshot we embedded; otherwise the
        # update trigger may already have invalidated its old vector.
        stored = conn.execute(
            "INSERT OR REPLACE INTO task_embeddings (task_id, embedding) "
            "SELECT id, ? FROM tasks WHERE id = ? AND title IS ? AND notes IS ?",
            (emb_bytes, task_id, title, notes),
        )
        conn.commit()  # release the writer lock before the next model call
        return True if stored.rowcount else None
    except Exception:
        return False


def backfill_task_embeddings(limit: int = 200) -> int:
    """Embed up to `limit` un-embedded tasks (most-recently-updated first).

    The startup worker uses backfill_all_task_embeddings in bounded batches.
    Ranked search also warms recent eligible tasks independently of FTS, so
    semantic coverage grows before the startup worker finishes. This function
    remains available for an explicit, limited warm-up
    (e.g. from a maintenance task or a one-off CLI run) ahead of query time.
    Returns the number of tasks embedded.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, notes FROM tasks WHERE "
            "id NOT IN (SELECT task_id FROM task_embeddings) "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        embedded = 0
        for row in rows:
            stored = _store_task_embedding(conn, row["id"], row["title"], row["notes"])
            if stored is False:
                break
            if stored is True:
                embedded += 1
        return embedded
    finally:
        conn.close()


def backfill_all_task_embeddings(batch_size: int = 32) -> int:
    """Warm legacy tasks in bounded batches on startup, resuming after restart.

    Each completed batch is committed. On model failure, stop rather than
    repeatedly attempting a download for every row; the next startup retries
    the first still-missing task. The id cursor avoids rescanning old rows on
    this run while the NOT IN guard makes subsequent runs resumable.
    """
    conn = storage.connect()
    embedded = 0
    after_id = 0
    try:
        while True:
            rows = conn.execute(
                "SELECT id, title, notes FROM tasks WHERE id > ? "
                "AND id NOT IN (SELECT task_id FROM task_embeddings) "
                "ORDER BY id LIMIT ?",
                (after_id, batch_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                stored = _store_task_embedding(conn, row["id"], row["title"], row["notes"])
                if stored is False:
                    return embedded
                if stored is True:
                    embedded += 1
            after_id = rows[-1]["id"]
        return embedded
    except sqlite3.Error:
        return embedded
    finally:
        conn.close()


def _semantic_scores(conn: sqlite3.Connection, emb_bytes: bytes) -> dict[int, float]:
    rows = conn.execute(
        "SELECT task_id, cosine_similarity(embedding, ?) AS similarity FROM task_embeddings",
        (emb_bytes,),
    ).fetchall()
    return {r["task_id"]: r["similarity"] for r in rows}


def ranked_task_search(
    query: str,
    where_sql: str,
    where_params: list[Any],
    limit: int = 20,
) -> dict:
    """Rank tasks matching `where_sql`/`where_params` by combined score.

    `where_sql`/`where_params` are task_list's own filter clause (status,
    project, domain, lane, priority, ...) built the same way for both the
    substring and ranked paths -- this function only decides which of those
    already-filtered rows come back first, never what qualifies as a filter.

    Returns {tasks: [{..., "_row": sqlite3.Row, "combined_score": float,
    "explanation": {...}}]} sorted by combined_score descending, capped to
    `limit` (1-100). The caller (task_list) is responsible for turning `_row`
    into the same public task shape task_list always returns.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        raise ToolError("query must contain at least one search term")
    tokens = cleaned.split()
    fts_query = _sanitize_fts_or_query(tokens)

    conn = storage.connect()
    try:
        fts_matches: dict[int, dict] = {}
        fts_truncated = False
        if fts_query:
            try:
                # Apply task_list's filters before the global candidate cap.
                # Otherwise a common term in another project can consume all
                # 200 FTS slots before an eligible task is even considered.
                eligible = (
                    f"SELECT id FROM tasks WHERE {where_sql}"
                    if where_sql
                    else "SELECT id FROM tasks"
                )
                rows = conn.execute(
                    "SELECT task_fts.rowid AS id, bm25(task_fts) AS score, "
                    "snippet(task_fts, 1, '[', ']', ' … ', 20) AS snip "
                    f"FROM task_fts JOIN ({eligible}) eligible ON eligible.id = task_fts.rowid "
                    "WHERE task_fts MATCH ? ORDER BY score LIMIT ?",
                    (*where_params, fts_query, FTS_CANDIDATE_CAP + 1),
                ).fetchall()
                fts_truncated = len(rows) > FTS_CANDIDATE_CAP
                for r in rows[:FTS_CANDIDATE_CAP]:
                    fts_matches[r["id"]] = {"score": r["score"], "snippet": r["snip"]}
            except sqlite3.Error:
                pass

        semantic_scores: dict[int, float] = {}
        has_semantic = False
        try:
            from .embeddings import embedding_to_bytes, generate_embedding

            query_emb_bytes = embedding_to_bytes(generate_embedding(cleaned))
            semantic_scores = _semantic_scores(conn, query_emb_bytes)
            has_semantic = True
        except Exception:
            query_emb_bytes = None

        # Warm a bounded set independently of FTS. A synonym-only query has
        # no lexical candidates, but must still discover recently changed
        # tasks whose embeddings have not been cached yet.
        if has_semantic and query_emb_bytes is not None:
            eligible_clause = f" AND ({where_sql})" if where_sql else ""
            recent = conn.execute(
                "SELECT id FROM tasks WHERE id NOT IN (SELECT task_id FROM task_embeddings) "
                f"{eligible_clause} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*where_params, LAZY_EMBED_CAP),
            ).fetchall()
            missing = list(
                dict.fromkeys(
                    [row["id"] for row in recent]
                    + [tid for tid in fts_matches if tid not in semantic_scores]
                )
            )[:LAZY_EMBED_CAP]
            if missing:
                placeholders = ",".join("?" for _ in missing)
                rows = conn.execute(
                    f"SELECT id, title, notes FROM tasks WHERE id IN ({placeholders})",
                    missing,
                ).fetchall()
                for row in rows:
                    _store_task_embedding(conn, row["id"], row["title"], row["notes"])
                conn.commit()
                semantic_scores.update(_semantic_scores(conn, query_emb_bytes))

        semantic_hit_ids = {tid for tid, sim in semantic_scores.items() if sim >= 0.55}
        candidate_ids = set(fts_matches) | semantic_hit_ids
        if not candidate_ids:
            return {"tasks": [], "truncated": False}

        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _ua_task_search_candidates (id INTEGER PRIMARY KEY)"
        )
        conn.execute("DELETE FROM _ua_task_search_candidates")
        conn.executemany(
            "INSERT OR IGNORE INTO _ua_task_search_candidates (id) VALUES (?)",
            ((task_id,) for task_id in candidate_ids),
        )
        full_where = "id IN (SELECT id FROM _ua_task_search_candidates)"
        params: list[Any] = []
        if where_sql:
            full_where += f" AND ({where_sql})"
            params += list(where_params)
        rows = conn.execute(f"SELECT * FROM tasks WHERE {full_where}", params).fetchall()
    finally:
        conn.close()

    w_sem = _get_weight("UA_TASK_SEARCH_WEIGHT_SEMANTIC", 1.0)
    w_fts = _get_weight("UA_TASK_SEARCH_WEIGHT_FTS", 0.5)
    w_rec = _get_weight("UA_TASK_SEARCH_WEIGHT_RECENCY", 0.3)
    half_life = _get_weight("UA_TASK_SEARCH_RECENCY_HALF_LIFE_DAYS", 15.0)
    if half_life <= 0:
        half_life = 15.0

    scored: list[tuple[float, sqlite3.Row, dict]] = []
    for row in rows:
        task_id = row["id"]
        fts_data = fts_matches.get(task_id)
        fts_score = fts_data["score"] if fts_data else None
        fts_score_norm = min(10.0, max(0.0, -fts_score)) / 10.0 if fts_score is not None else 0.0
        similarity = semantic_scores.get(task_id, 0.0) if has_semantic else 0.0
        is_semantic_match = has_semantic and similarity >= 0.55

        try:
            updated_dt = datetime.fromisoformat(row["updated_at"])
            age_days = max(0.0, (datetime.now(UTC) - updated_dt).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0
        recency_decay = 2.0 ** (-age_days / half_life)

        combined_score = w_sem * similarity + w_fts * fts_score_norm + w_rec * recency_decay

        why = []
        if fts_data is not None:
            why.append("lexical")
        if is_semantic_match:
            why.append("semantic")
        if not why:
            why.append("recency")

        explanation = {
            "why": why,
            "lexical": round(fts_score_norm, 4),
            "semantic": round(similarity, 4) if has_semantic else None,
            "recency": round(recency_decay, 4),
            "salience": None,
            "access": None,
            "not_applicable": list(NOT_APPLICABLE),
            "snippet": fts_data["snippet"] if fts_data else None,
        }
        scored.append((combined_score, row, explanation))

    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    capped = scored[: max(1, min(limit, 100))]
    return {
        "truncated": fts_truncated or len(scored) > len(capped),
        "tasks": [
            {"_row": row, "combined_score": round(score, 6), "explanation": explanation}
            for score, row, explanation in capped
        ],
    }
