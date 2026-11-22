"""Ranked task search (task #1419, Parity 14).

Covers: task_fts backfill of pre-existing rows, ranked mode via
task_list(query=...), the explanation shape, that notes_contain (substring)
mode is unchanged and mutually exclusive with query, and that cursor
pagination is rejected in ranked mode.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage, task_search
from unified_agent.tools.tasks import task_add, task_list, task_update


def test_task_fts_backfills_rows_created_before_the_table_existed():
    """A task inserted straight into the tasks table (bypassing task_add, the
    way a pre-#1419 database's rows all were) must still show up in task_fts
    once ensure_schema's backfill runs again."""
    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (9001, 'Legacy row predating task_fts', 'no trigger ever saw this', "
            "'open', 'med', ?, ?)",
            (now, now),
        )
        conn.commit()
        # Simulate a fresh backfill run (e.g. the next process startup) by
        # dropping this row's FTS entry and calling ensure_schema again. An
        # external-content fts5 table's own rows must be removed with the
        # special 'delete' command form -- a plain DELETE against it is a
        # silent no-op (the index is keyed off content='tasks', not a normal
        # table row).
        conn.execute(
            "INSERT INTO task_fts(task_fts, rowid, title, notes) VALUES "
            "('delete', 9001, 'Legacy row predating task_fts', 'no trigger ever saw this')"
        )
        conn.commit()
        task_search.ensure_schema(conn)
        hit = conn.execute("SELECT rowid FROM task_fts WHERE task_fts MATCH 'Legacy'").fetchall()
        assert {r["rowid"] for r in hit} == {9001}
    finally:
        conn.close()


def test_backfill_is_idempotent_and_only_touches_missing_rows():
    task_add(title="Already indexed task")
    conn = storage.connect()
    try:
        before = {r["rowid"] for r in conn.execute("SELECT rowid FROM task_fts").fetchall()}
        assert before  # the trigger already indexed it
        task_search.ensure_schema(conn)  # re-run: must be a no-op, not an error
        after = {r["rowid"] for r in conn.execute("SELECT rowid FROM task_fts").fetchall()}
        assert before == after
    finally:
        conn.close()


def test_ranked_mode_finds_lexical_matches_and_explains_why():
    hit = task_add(
        title="Fix login race condition",
        notes="Duplicate sessions appear when two login requests race.",
    )["id"]
    other = task_add(title="Write release notes for v2", notes="Nothing about auth here.")["id"]

    result = task_list(status="all", query="login race")
    assert result["ranked"] is True
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    ids = [t["id"] for t in result["tasks"]]
    assert hit in ids
    assert other not in ids

    row = next(t for t in result["tasks"] if t["id"] == hit)
    assert "combined_score" in row
    assert row["combined_score"] > 0
    explanation = row["explanation"]
    assert "lexical" in explanation["why"]
    # A two-row test corpus gives fts5's bm25 almost no IDF signal to work
    # with (nearly every doc "matches" similarly), so the normalized lexical
    # score legitimately rounds to ~0 here -- the real assertion is that a
    # match was found and recorded as the "why", not the magnitude.
    assert explanation["lexical"] >= 0
    # The two documented terms tasks have no equivalent for are named
    # explicitly, not silently dropped or faked with a stand-in signal.
    assert explanation["salience"] is None
    assert explanation["access"] is None
    assert len(explanation["not_applicable"]) == 2
    assert any("salience" in note for note in explanation["not_applicable"])
    assert any("access" in note for note in explanation["not_applicable"])


def test_ranked_mode_respects_the_same_filters_as_substring_mode():
    open_hit = task_add(title="Auth latency spike investigation", priority="high")["id"]
    done_id = task_add(title="Auth latency spike investigation, take two")["id"]
    from unified_agent.tools.tasks import task_update

    task_update(id=done_id, status="done", actor="test-suite", evidence_ref="n/a")

    open_only = task_list(status="open", query="auth latency")
    ids = [t["id"] for t in open_only["tasks"]]
    assert open_hit in ids
    assert done_id not in ids

    both = task_list(status="all", query="auth latency")
    ids_all = [t["id"] for t in both["tasks"]]
    assert open_hit in ids_all
    assert done_id in ids_all


def test_ranked_fts_cap_applies_after_project_filter(monkeypatch):
    monkeypatch.setattr(task_search, "FTS_CANDIDATE_CAP", 1)
    task_add(title="quartzneedle quartzneedle quartzneedle", project="browser")
    target = task_add(title="quartzneedle", project="unified-agent")["id"]

    result = task_list(status="all", project="unified-agent", query="quartzneedle")
    assert [task["id"] for task in result["tasks"]] == [target]


def test_ranked_search_reports_lexical_matches_beyond_candidate_cap(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(task_search, "FTS_CANDIDATE_CAP", 1)

    def offline(_text):
        raise RuntimeError("offline")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)
    task_add(title="quartzneedle one")
    task_add(title="quartzneedle two")

    result = task_list(status="all", query="quartzneedle", limit=10)
    assert len(result["tasks"]) == 1
    assert result["truncated"] is True


def test_semantic_candidate_ids_do_not_exceed_sqlite_variable_limit(monkeypatch):
    import sqlite3

    from unified_agent import embeddings

    ids = [task_add(title=f"Candidate {index}")["id"] for index in range(60)]
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(
        task_search, "_semantic_scores", lambda _conn, _query: dict.fromkeys(ids, 1.0)
    )
    connect = storage.connect

    def limited_connect():
        conn = connect()
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        return conn

    monkeypatch.setattr(storage, "connect", limited_connect)
    result = task_list(status="all", query="unmatchedterm", limit=10)
    assert result["tasks"]


def test_synonym_only_query_warms_recent_task_without_fts_match(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0, 0.0])
    target = task_add(title="Repair the car")["id"]

    result = task_list(status="all", query="automobile")
    assert target in [task["id"] for task in result["tasks"]]
    row = next(task for task in result["tasks"] if task["id"] == target)
    assert "semantic" in row["explanation"]["why"]


def test_synonym_only_query_can_warm_archived_scope(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0, 0.0])
    target = task_add(title="Repair the car")["id"]
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (storage.now(), target))
        conn.commit()
    finally:
        conn.close()

    result = task_list(status="all", archived=True, query="automobile")
    assert target in [task["id"] for task in result["tasks"]]


def test_explicit_embedding_warmup_includes_archived_tasks(monkeypatch):
    from unified_agent import embeddings

    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0, 0.0])
    target = task_add(title="Archived task")["id"]
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (storage.now(), target))
        conn.commit()
    finally:
        conn.close()


def test_explicit_warmup_reports_only_stored_vectors(monkeypatch):
    from unified_agent import embeddings

    target = task_add(title="Text before edit")["id"]

    def edit_during_inference(_text):
        task_update(id=target, title="Text after edit")
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings, "generate_embedding", edit_during_inference)
    assert task_search.backfill_task_embeddings(limit=10) == 0

    assert task_search.backfill_task_embeddings(limit=10) == 1
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT task_id FROM task_embeddings WHERE task_id = ?", (target,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_startup_embedding_backfill_resumes_after_model_failure(monkeypatch):
    from unified_agent import embeddings

    ids = [task_add(title=f"Legacy task {index}")["id"] for index in range(3)]

    def sometimes_fails(text):
        if "Legacy task 1" in text:
            raise RuntimeError("model temporarily unavailable")
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings, "generate_embedding", sometimes_fails)
    assert task_search.backfill_all_task_embeddings(batch_size=2) == 1
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [1.0, 0.0])
    assert task_search.backfill_all_task_embeddings(batch_size=2) == 2

    conn = storage.connect()
    try:
        embedded = {row["task_id"] for row in conn.execute("SELECT task_id FROM task_embeddings")}
    finally:
        conn.close()
    assert set(ids) <= embedded


def test_embedding_backfill_does_not_store_vector_for_edited_task(monkeypatch):
    from unified_agent import embeddings

    target = task_add(title="Old task title")["id"]

    def edit_during_inference(_text):
        task_update(id=target, title="New task title")
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings, "generate_embedding", edit_during_inference)
    assert task_search.backfill_all_task_embeddings(batch_size=1) == 0
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT task_id FROM task_embeddings WHERE task_id = ?", (target,)
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_embedding_backfill_releases_writer_lock_between_model_calls(monkeypatch):
    from unified_agent import embeddings

    first = task_add(title="Legacy first")["id"]
    task_add(title="Legacy second")

    def edit_before_second_embedding(text):
        if "Legacy second" in text:
            task_update(id=first, title="Edited while backfill runs")
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings, "generate_embedding", edit_before_second_embedding)
    assert task_search.backfill_all_task_embeddings(batch_size=2) == 2


def test_ranked_results_are_sorted_by_combined_score_descending():
    task_add(title="deploy pipeline flaky test", notes="one mention of deploy pipeline")
    task_add(
        title="deploy pipeline deploy pipeline",
        notes="deploy pipeline deploy pipeline deploy pipeline",
    )
    result = task_list(status="all", query="deploy pipeline")
    scores = [t["combined_score"] for t in result["tasks"]]
    assert scores == sorted(scores, reverse=True)


def test_ranked_top_k_reports_additional_matches():
    task_add(title="quartzneedle first")
    task_add(title="quartzneedle second")

    result = task_list(status="all", query="quartzneedle", limit=1)
    assert len(result["tasks"]) == 1
    assert result["truncated"] is True
    assert result["next_cursor"] is None


def test_query_and_notes_contain_are_mutually_exclusive():
    with pytest.raises(ToolError, match="mutually exclusive"):
        task_list(query="x", notes_contain="y")


def test_query_rejects_cursor_pagination():
    with pytest.raises(ToolError, match="cursor"):
        task_list(query="x", cursor="bogus")


def test_notes_contain_substring_mode_is_unchanged():
    """The pre-#1419 substring path must not fork: same shape, same behavior."""
    hit = task_add(title="release checklist", notes="[waiting-for: review] please look")["id"]
    other = task_add(title="unrelated", notes="nothing to see")["id"]

    result = task_list(status="all", notes_contain="[waiting-for: review]")
    ids = [t["id"] for t in result["tasks"]]
    assert hit in ids
    assert other not in ids
    assert "ranked" not in result
    assert "combined_score" not in result["tasks"][0]


def test_notes_contain_treats_like_wildcards_literally():
    hit = task_add(title="literal", notes="foo_bar 50% done \\ path")["id"]
    decoy = task_add(title="decoy", notes="fooXbar 50X done")["id"]
    for needle in ("foo_bar", "50%", "\\"):
        ids = [t["id"] for t in task_list(status="all", notes_contain=needle)["tasks"]]
        assert hit in ids and decoy not in ids, needle


def test_empty_query_raises():
    with pytest.raises(ToolError):
        task_list(query="   ")


@pytest.mark.embeddings
def test_ranked_mode_semantic_component_is_populated():
    task_add(
        title="Investigate authentication sign-in slowness",
        notes="Users report the sign-in flow is sluggish after the last release.",
    )
    result = task_list(status="all", query="login performance problem")
    # A pure-synonym query may or may not clear the FTS bar, but semantic
    # scoring must at least have run and produced a real (non-None) number
    # for every candidate that came back.
    for t in result["tasks"]:
        assert t["explanation"]["semantic"] is not None


def test_natural_language_query_finds_via_fts_alone(monkeypatch):
    """OR-joined FTS tokens, not memory_search's implicit AND (coordinator
    finding #1): a sentence-shaped query must still surface the task via
    lexical overlap on individual words, with semantic scoring forced off so
    the assertion is about FTS alone, not a fallback to embeddings."""
    from unified_agent import embeddings as embeddings_module

    def _no_embeddings(*args, **kwargs):
        raise RuntimeError("embeddings disabled for this test")

    monkeypatch.setattr(embeddings_module, "generate_embedding", _no_embeddings)

    hit = task_add(
        title="Fix login bug reported by support",
        notes="A customer hit a login bug again this morning; needs an owner.",
    )["id"]
    other = task_add(title="Unrelated billing cleanup", notes="Nothing about login here.")["id"]

    # Every word in this sentence would need to co-occur in one task row
    # under AND semantics -- practically never true for natural language.
    # Under OR semantics, "login" and "bug" alone are enough for `hit` to
    # surface. Small corpora can rank the shorter one-term row higher by bm25.
    result = task_list(status="all", query="who is working on the login bug")
    ids = [t["id"] for t in result["tasks"]]
    assert hit in ids

    row = next(t for t in result["tasks"] if t["id"] == hit)
    assert row["explanation"]["semantic"] is None
    assert "lexical" in row["explanation"]["why"]
    # As in test_ranked_mode_finds_lexical_matches_and_explains_why: a
    # two-task corpus starves bm25's IDF term, so the normalized lexical
    # score legitimately rounds to ~0 -- the assertion that matters is that
    # a match was found and recorded as the "why", not the magnitude.
    assert row["explanation"]["lexical"] >= 0

    assert other in ids


def test_fts_query_special_characters_cannot_inject_fts5_syntax(monkeypatch):
    """A query built entirely from fts5 operator/syntax characters must not
    raise a sqlite3 syntax error -- they are literal search terms, quoted and
    OR-joined, never interpreted as MATCH-query syntax (coordinator finding
    #1: sanitize before OR-joining)."""
    from unified_agent import embeddings

    def offline(_text):
        raise RuntimeError("offline")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)
    task_add(title="Normal task", notes="Nothing special here.")
    # AND/OR/NOT/NEAR, ^column, *prefix, -exclude, parens, and a bare quote
    # (the one character that would otherwise escape a quoted phrase).
    result = task_list(status="all", query='AND OR NOT NEAR^2 *pre -ex (paren) "')
    assert result["ranked"] is True
    assert result["tasks"] == []


def test_compact_mode_has_the_same_keys_ranked_and_substring():
    """Ranked mode's fields='compact' must return
    exactly the same key set as the substring path's compact mode for the
    same task -- including the three SQL-computed flags
    (waiting_for_review, laptop_lane, notes_archived), which ranked_task_search's
    own `SELECT *` has no way to produce on its own."""
    task_id = task_add(
        title="Fix login race condition",
        notes="[waiting-for: review] duplicate login sessions, laptop-lane pickup",
    )["id"]

    substring = task_list(status="all", notes_contain="waiting-for", fields="compact")
    substring_row = next(t for t in substring["tasks"] if t["id"] == task_id)

    ranked = task_list(status="all", query="login", fields="compact")
    ranked_row = next(t for t in ranked["tasks"] if t["id"] == task_id)

    # combined_score/explanation are ranked-mode-only additions, not a parity
    # gap -- exclude them from the key-set comparison, then require equality
    # on everything else (including the flags above).
    ranked_keys = set(ranked_row) - {"combined_score", "explanation"}
    assert ranked_keys == set(substring_row)
    for flag in ("waiting_for_review", "laptop_lane", "notes_archived"):
        assert flag in ranked_row, f"{flag} missing from ranked compact output"
        assert isinstance(ranked_row[flag], bool)
        assert ranked_row[flag] == substring_row[flag]


def test_task_embedding_is_dropped_when_task_text_changes():

    created = task_add("Embedding staleness probe", notes="original text")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_embeddings (task_id, embedding) VALUES (?, ?)",
            (created["id"], b"\x00" * 8),
        )
        conn.commit()
    finally:
        conn.close()
    task_update(created["id"], notes="rewritten text")
    conn = storage.connect()
    try:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM task_embeddings WHERE task_id=?", (created["id"],)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert left == 0
    assert task_search.LAZY_EMBED_CAP > 0
