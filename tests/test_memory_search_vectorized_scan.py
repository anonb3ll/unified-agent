"""Regression tests for task #1001: memory_search's candidate-scoring pass.

Task #967's 250K/500K benchmark (docs/designs/memory-search-scale-benchmark-
250k-500k.md) found `memory_search` fetching essentially the entire live table
with `SELECT * FROM memory{where_clause}` and running the full Python scoring
loop (JSON tag decode, entity-extraction regex, weighted-score formula) over
every row, even though the vast majority never survive the
`is_fts_match or is_semantic_match or is_identifier_match` gate. That made the
candidate-scoring pass O(N) in table size (p50 14.8s at 250K notes, 29.3s at
500K) rather than O(candidates).

The fix restricts the SQL fetch to the union of FTS hits, strong semantic
hits, and (for task/branch queries) a safe substring-widened identifier scan,
so Python only scores rows that could possibly qualify.

These tests prove two things:
  1. Candidate parity — the restricted fetch keeps exactly the notes a
     full-table scan would (the frozen pre-change score formula was retired
     when task #1913 moved ranking to reciprocal rank fusion).
  2. No full-table-scan regression — the number of rows the SQL layer
     materializes scales with the candidate set, not with table size.
"""

from unified_agent import storage
from unified_agent.tools.memory import memory_save, memory_search

# ---------------------------------------------------------------------------
# 1. Candidate parity against a full-table reference.
# ---------------------------------------------------------------------------
#
# Task #1913 replaced the weighted-sum score with reciprocal rank fusion, so
# the frozen pre-#1001 score formula that used to live here no longer applies.
# The #1001 invariant still does: restricting the SQL fetch to FTS hits,
# semantic-leg hits and identifier LIKE matches must never lose a note the
# match gate would keep. The reference below walks the *whole* table in
# Python and applies the gate exactly; memory_search must return the same
# set. Fusion ranks the entity and recency legs among the surviving
# candidates, so an equal candidate set also means equal scores and order.


def _full_scan_candidate_ids(query: str, tags=None) -> set[str]:
    import json

    from unified_agent import edition
    from unified_agent import memory_ranking as ranking
    from unified_agent.tools import memory as m

    cleaned = query.replace('"', " ").strip()
    entities = m._extract_query_entities(cleaned)
    conn = storage.connect()
    try:
        fts = ranking.lexical_leg(conn, cleaned.split(), edition.tenant_org_id())
        rows = conn.execute("SELECT * FROM memory WHERE superseded_by IS NULL").fetchall()
    finally:
        conn.close()
    keep = set()
    for row in rows:
        row_tags = set(json.loads(row["tags"]))
        if m._is_index_id(row["id"]) or m.DELETED_TAG in row_tags:
            continue
        if tags and not set(tags).issubset(row_tags):
            continue
        matches = m._match_query_entities(
            entities,
            title=row["title"] or "",
            content=row["content"] or "",
            tags=row_tags,
            source_host=row["source_host"],
            author=row["author"],
        )
        identifier = any(x["kind"] in m._IDENTIFIER_ENTITY_KINDS for x in matches)
        if row["id"] in fts or identifier:
            keep.add(row["id"])
    return keep


def _seed_corpus():
    memory_save(
        "The database migration for the auth service is now complete.",
        title="Auth DB migration",
        tags=["database", "auth"],
    )
    memory_save(
        "Notes about the frontend redesign and the new database schema.",
        title="Frontend redesign",
        tags=["frontend", "database"],
    )
    memory_save(
        "Investigating a slow database query on the reporting pipeline.",
        title="Reporting perf",
        tags=["database", "perf"],
    )
    memory_save(
        "Meeting notes: roadmap planning for Q3, no database content here.",
        title="Q3 roadmap",
        tags=["planning"],
    )
    memory_save(
        "task/1001-memory-search-vectorize is in progress on the desktop agent.",
        title="Task 1001 status",
        tags=["work"],
    )
    memory_save(
        "Unrelated note about lunch plans and weekend hiking trails.",
        title="Weekend plans",
        tags=["personal"],
    )


def test_memory_search_keeps_every_full_scan_candidate(isolated_env, monkeypatch):
    """The candidate-restricted fetch returns exactly the notes a full-table
    scan would keep, for plain FTS, tag-filtered, multi-word (OR fallback)
    and identifier-only query shapes. Embeddings are off so the gate is
    lexical plus identifier and fully deterministic."""
    from unified_agent import embeddings

    def _offline(_text):
        raise RuntimeError("offline")

    monkeypatch.setattr(embeddings, "generate_embedding", _offline)
    _seed_corpus()

    for query, tags in [
        ("database", None),
        ("database", ["database"]),
        ("database migration auth service", None),
        ("slow database migration for the roadmap", None),
        ("task 1001", None),
    ]:
        expected = _full_scan_candidate_ids(query, tags=tags)
        assert expected, f"reference found nothing for {query!r}"
        actual = memory_search(query, tags=tags, limit=50)["results"]
        assert {r["id"] for r in actual} == expected, f"candidate mismatch for {query!r}"
        scores = [r["combined_score"] for r in actual]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# 2. Regression guard: candidate materialization must not scale with table
#    size — it must scale with the FTS/semantic/identifier candidate set.
# ---------------------------------------------------------------------------


def test_memory_search_does_not_scan_the_full_table(isolated_env, monkeypatch):
    """Seed a corpus where only a handful of notes could ever match a query,
    and assert the SQL layer never materializes anywhere near the full table.

    This is the regression test for the #1001 bug: the old code ran
    `SELECT * FROM memory{where_clause}` unconditionally, so this test would
    have failed against it (rows_fetched == total live notes, not the tiny
    candidate set)."""
    total_notes = 250
    matching = 5
    for i in range(total_notes):
        if i < matching:
            memory_save(
                f"A unique needle-marker-zzq term appears in note {i}.",
                title=f"Needle note {i}",
            )
        else:
            memory_save(
                f"Ordinary filler content about topic number {i} with no relation.",
                title=f"Filler note {i}",
            )

    conn = storage.connect()
    total_live = conn.execute(
        "SELECT COUNT(*) AS n FROM memory WHERE superseded_by IS NULL"
    ).fetchone()["n"]
    conn.close()
    assert total_live >= total_notes  # sanity: corpus actually landed

    # sqlite3.Connection is a C-level immutable type, so its `execute` method
    # can't be monkeypatched directly. Instead, wrap the real connection
    # storage.connect() returns in a thin Python proxy that intercepts
    # `.execute()` to count rows for the one query shape we care about (the
    # unfiltered `SELECT * FROM memory...` candidate fetch) and delegates
    # everything else untouched.
    captured: list[int] = []

    class _ReplayCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _ConnSpy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            cur = self._real.execute(sql, *args, **kwargs)
            if "FROM memory" in sql and "SELECT *" in sql and "memory_fts" not in sql:
                rows = cur.fetchall()
                captured.append(len(rows))
                return _ReplayCursor(rows)
            return cur

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = storage.connect

    def spy_connect():
        return _ConnSpy(real_connect())

    monkeypatch.setattr(storage, "connect", spy_connect)
    try:
        result = memory_search("needle-marker-zzq")
    finally:
        monkeypatch.setattr(storage, "connect", real_connect)

    assert [r["id"] for r in result["results"]]
    assert len(result["results"]) == matching

    assert captured, "expected the candidate SELECT * FROM memory query to run"
    rows_fetched = captured[0]
    # The candidate-restricted query must fetch (at most, allowing a little
    # slack for auto-maintained index notes) the handful of true matches —
    # nowhere near the full live table.
    assert rows_fetched <= matching + 5, (
        f"memory_search materialized {rows_fetched} rows out of {total_live} "
        "live notes for a query with only 5 possible matches — looks like the "
        "O(N) full-table scan (task #1001) regressed"
    )
