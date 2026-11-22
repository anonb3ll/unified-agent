"""Evolving confidence and note versions (task #2097, epic #1915 M7 + M8).

M7: ``memory_feedback`` counts are the evidence of a per-note Beta posterior.
An open ``contradicts`` link lowers the note's effective confidence at read
time (nothing stored changes), and the effective confidence is the feedback
input of ``memory_ranking.prior_multiplier`` inside its [0.8, 1.2] clamp.

M8: a SQLite trigger copies a note's previous content and tags into
``memory_versions`` whenever either changes, whichever path wrote it.
``memory_get(include_versions=True)`` returns them newest first, bounded.
A hard delete erases the versions with the note.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, consolidate, embeddings, memory_ranking, storage, table_schemas
from unified_agent.tools import memory as memory_tools


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    def _offline(_text: str):
        raise RuntimeError("lexical-only test")

    monkeypatch.setattr(embeddings, "generate_embedding", _offline)


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _save(title: str, content: str, **kwargs) -> str:
    kwargs.setdefault("author", "Desktop_Agent2")
    return memory_tools.memory_save(content=content, title=title, **kwargs)["id"]


def _sql(sql: str, params: tuple = ()) -> list[tuple]:
    conn = storage.connect()
    try:
        rows = [tuple(r) for r in conn.execute(sql, params).fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


def _versions(note_id: str) -> list[tuple]:
    return _sql(
        "SELECT version, content, tags, actor FROM memory_versions WHERE note_id=? "
        "ORDER BY version",
        (note_id,),
    )


def _hit(query: str, note_id: str, **kwargs) -> dict | None:
    for hit in memory_tools.memory_search(query, **kwargs)["results"]:
        if hit["id"] == note_id:
            return hit
    return None


# --- M7: Beta posterior -------------------------------------------------------


def test_posterior_mapping_is_documented_increments():
    assert memory_ranking.confidence_posterior(0, 0, 0, 0) == (1.0, 1.0)
    assert memory_ranking.confidence_posterior(3, 0, 0, 0) == (4.0, 1.0)
    assert memory_ranking.confidence_posterior(0, 2, 0, 0) == (1.0, 3.0)
    assert memory_ranking.confidence_posterior(0, 0, 2, 0) == (1.0, 2.0)
    assert memory_ranking.confidence_posterior(0, 0, 0, 1) == (1.0, 3.0)
    # Each signal's increment is exactly the documented table.
    for signal, (da, db) in memory_ranking.FEEDBACK_INCREMENTS.items():
        counts = {s: int(s == signal) for s in memory_ranking.FEEDBACK_INCREMENTS}
        alpha, beta = memory_ranking.confidence_posterior(
            counts["helpful"], counts["not_helpful"], counts["stale"], counts["wrong"]
        )
        assert (alpha, beta) == (1.0 + da, 1.0 + db)


def test_effective_confidence_drops_per_open_contradiction_and_is_capped():
    base = memory_ranking.effective_confidence(1.0, 1.0, 0)
    assert base == 0.5
    one = memory_ranking.effective_confidence(1.0, 1.0, 1)
    two = memory_ranking.effective_confidence(1.0, 1.0, 2)
    assert base > one > two > 0
    cap = memory_ranking.MAX_COUNTED_CONTRADICTIONS
    assert memory_ranking.effective_confidence(
        1.0, 1.0, cap + 50
    ) == memory_ranking.effective_confidence(1.0, 1.0, cap)


@pytest.mark.parametrize("confidence", [0.0, 0.01, 0.5, 0.99, 1.0])
def test_confidence_feeds_the_prior_inside_the_unchanged_clamp(confidence):
    prior, inputs = memory_ranking.prior_multiplier(
        salience=0.0 if confidence < 0.5 else 1.0,
        confidence=confidence,
        vote=0.0,
        age_discount=1.0,
        w_salience=5.0,
        w_feedback=5.0,
        w_vote=0.0,
    )
    assert memory_ranking.PRIOR_MIN == 0.8 and memory_ranking.PRIOR_MAX == 1.2
    assert 0.8 <= prior <= 1.2
    assert "confidence" in inputs and "feedback" not in inputs
    assert inputs["confidence"] == pytest.approx(5.0 * (2 * confidence - 1))


def test_neutral_note_has_a_zero_confidence_input():
    prior, inputs = memory_ranking.prior_multiplier(
        salience=None,
        confidence=0.5,
        vote=0.0,
        age_discount=1.0,
        w_salience=0.5,
        w_feedback=0.4,
        w_vote=0.3,
    )
    assert inputs["confidence"] == 0.0
    assert prior == 1.0


def test_feedback_updates_the_posterior_without_editing_the_note():
    nid = _save("Pangolin cache", "Pangolin cache lives in the edge tier.")
    start = memory_tools.memory_get(nid)["confidence_posterior"]
    assert start == {
        "alpha": 1.0,
        "beta": 1.0,
        "posterior": 0.5,
        "open_contradictions": 0,
        "effective": 0.5,
    }
    out = memory_tools.memory_feedback(nid, "helpful")
    assert out["confidence_posterior"]["alpha"] == 2.0
    memory_tools.memory_feedback(nid, "wrong")
    memory_tools.memory_feedback(nid, "stale")
    got = memory_tools.memory_get(nid)["confidence_posterior"]
    assert (got["alpha"], got["beta"]) == (2.0, 3.5)
    assert got["posterior"] == pytest.approx(2.0 / 5.5, abs=1e-4)
    # Feedback is not an edit: no version row, content unchanged.
    assert _versions(nid) == []


def test_open_contradiction_lowers_effective_confidence_at_read_time():
    disputed = _save("Ocelot port", "Ocelot listens on port 7000.")
    disputer = _save("Ocelot port correction", "Ocelot moved to 7100.", contradicts=[disputed])
    got = memory_tools.memory_get(disputed)["confidence_posterior"]
    assert got["open_contradictions"] == 1
    assert got["posterior"] == 0.5
    assert got["effective"] < got["posterior"]
    # Only the disputed note is lowered; the disputer is not.
    assert memory_tools.memory_get(disputer)["confidence_posterior"]["open_contradictions"] == 0
    # Nothing stored changed on the disputed note.
    row = _sql(
        "SELECT feedback_helpful, feedback_not_helpful, feedback_stale, feedback_wrong, "
        "confidence FROM memory WHERE id=?",
        (disputed,),
    )
    assert row == [(0, 0, 0, 0, None)]


def test_contradiction_closes_when_the_disputer_is_cleared_superseded_deleted_or_expired(
    monkeypatch,
):
    def _open(nid: str) -> int:
        return memory_tools.memory_get(nid)["confidence_posterior"]["open_contradictions"]

    target = _save("Quokka quota", "Quokka quota is 10 GB.")

    cleared = _save("Quokka dispute A", "Quota is 12 GB.", contradicts=[target])
    assert _open(target) == 1
    memory_tools.memory_save(
        content="Quota is 12 GB.", id=cleared, author="Desktop_Agent2", contradicts=[]
    )
    assert _open(target) == 0

    superseded = _save("Quokka dispute B", "Quota is 14 GB.", contradicts=[target])
    assert _open(target) == 1
    _save("Quokka dispute B, withdrawn", "I was wrong about the quota.", supersedes=superseded)
    assert _open(target) == 0

    deleted = _save("Quokka dispute C", "Quota is 16 GB.", contradicts=[target])
    assert _open(target) == 1
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
    assert memory_tools.memory_delete(deleted)["deleted"]
    assert _open(target) == 0

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    expired = _save("Quokka dispute D", "Quota is 18 GB.", contradicts=[target])
    assert _open(target) == 1
    _sql("UPDATE memory SET expires_at=? WHERE id=?", (past, expired))
    assert _open(target) == 0


def test_contradiction_lowers_search_rank_and_explains_it():
    # Twins; without the contradiction the disputed one wins the title tie-break.
    plain = _save("Note B", "Wombat deploy window is Tuesday morning.")
    disputed = _save("Note A", "Wombat deploy window is Tuesday morning.")
    _save("Unrelated correction", "The window moved to Thursday.", contradicts=[disputed])
    results = memory_tools.memory_search("wombat deploy window")["results"]
    ids = [r["id"] for r in results]
    assert ids.index(plain) < ids.index(disputed)
    expl = next(r for r in results if r["id"] == disputed)["explanation"]
    assert expl["prior_inputs"]["confidence"] < 0
    assert expl["confidence_posterior"]["open_contradictions"] == 1
    assert 0.8 <= expl["prior"] < 1.0
    plain_expl = next(r for r in results if r["id"] == plain)["explanation"]
    assert plain_expl["prior_inputs"]["confidence"] == 0.0


def test_many_contradictions_and_wrong_feedback_never_breach_the_clamp(monkeypatch):
    # Saturate the inputs: the clamp, not the weights, is what bounds the prior.
    monkeypatch.setenv("UA_MEMORY_WEIGHT_FEEDBACK", "50")
    monkeypatch.setenv("UA_MEMORY_WEIGHT_SALIENCE", "50")
    target = _save("Kiwi build cache", "Kiwi build cache is on the fast disk.")
    for i in range(12):
        _save(f"Dispute {i}", f"Disagreement number {i}.", contradicts=[target])
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET feedback_wrong=500, salience=0.0 WHERE id=?", (target,))
        conn.commit()
    finally:
        conn.close()
    hit = _hit("kiwi build cache", target)
    assert hit is not None
    assert hit["explanation"]["prior"] >= memory_ranking.PRIOR_MIN
    assert hit["explanation"]["prior"] == pytest.approx(memory_ranking.PRIOR_MIN, abs=1e-3)
    assert (
        hit["explanation"]["confidence_posterior"]["open_contradictions"]
        == memory_ranking.MAX_COUNTED_CONTRADICTIONS
    )


def test_contradiction_counts_under_as_of_only_once_recorded_and_valid():
    target = _save("Lemur endpoint", "Lemur endpoint is /v1.")
    disputer = _save("Lemur correction", "Lemur endpoint is /v2.", contradicts=[target])
    _sql("UPDATE memory SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", target))
    as_of = "2026-02-01T00:00:00+00:00"
    # The disputer and its link were recorded now, after as_of.
    then = memory_tools.memory_get(target, as_of=as_of)["confidence_posterior"]
    assert then["open_contradictions"] == 0
    assert memory_tools.memory_get(target)["confidence_posterior"]["open_contradictions"] == 1
    # Back-date both: the contradiction was open at as_of.
    _sql("UPDATE memory SET created_at=? WHERE id=?", ("2026-01-15T00:00:00+00:00", disputer))
    _sql(
        "UPDATE memory_links SET created_at=? WHERE src_id=? AND rel='contradicts'",
        ("2026-01-15T00:00:00+00:00", disputer),
    )
    then = memory_tools.memory_get(target, as_of=as_of)["confidence_posterior"]
    assert then["open_contradictions"] == 1
    hit = _hit("lemur endpoint", target, as_of=as_of)
    assert hit["explanation"]["confidence_posterior"]["open_contradictions"] == 1


def _crowd(target: str, closed: int, kept: int) -> tuple[list[str], list[str]]:
    """``kept`` older and ``closed`` newer disputers of ``target``."""
    old = [
        _save(f"Old dispute {i}", f"Older objection {i}.", contradicts=[target])
        for i in range(kept)
    ]
    new = [
        _save(f"New dispute {i}", f"Newer objection {i}.", contradicts=[target])
        for i in range(closed)
    ]
    for nid in old:
        _sql(
            "UPDATE memory_links SET created_at=? WHERE src_id=? AND rel='contradicts'",
            ("2026-01-15T00:00:00+00:00", nid),
        )
        _sql("UPDATE memory SET created_at=? WHERE id=?", ("2026-01-15T00:00:00+00:00", nid))
    return old, new


def test_closed_newer_links_do_not_crowd_out_older_open_ones():
    """Review P3: filters run before any cap, so 25 newer closed links hide nothing."""
    target = _save("Puma registry", "Puma registry is on host blue.")
    _old, new = _crowd(target, closed=25, kept=3)
    # Expired: closed, and (before the fix) filtered only after the newest-20 cut.
    for nid in new:
        _sql("UPDATE memory SET expires_at=? WHERE id=?", ("2026-01-02T00:00:00+00:00", nid))
    got = memory_tools.memory_get(target)["confidence_posterior"]
    assert got["open_contradictions"] == 3


def test_as_of_counts_older_links_past_many_newer_ones():
    target = _save("Puma registry", "Puma registry is on host blue.")
    _sql("UPDATE memory SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", target))
    _crowd(target, closed=25, kept=3)
    now = memory_tools.memory_get(target)["confidence_posterior"]
    assert now["open_contradictions"] == memory_ranking.MAX_COUNTED_CONTRADICTIONS
    then = memory_tools.memory_get(target, as_of="2026-02-01T00:00:00+00:00")
    assert then["confidence_posterior"]["open_contradictions"] == 3


# --- M8: versions -------------------------------------------------------------


def test_update_records_the_prior_version():
    nid = _save("Heron rota", "Heron rota v1.", tags=["project:heron"])
    memory_tools.memory_save(content="Heron rota v2.", id=nid, author="Desktop_Agent2")
    rows = _versions(nid)
    assert len(rows) == 1
    version, content, tags, actor = rows[0]
    assert (version, content, json.loads(tags), actor) == (
        1,
        "Heron rota v1.",
        ["project:heron"],
        "Desktop_Agent2",
    )
    got = memory_tools.memory_get(nid, include_versions=True)
    assert got["content"] == "Heron rota v2."
    assert got["versions_total"] == 1
    assert got["versions"][0]["version"] == 1
    assert got["versions"][0]["content"] == "Heron rota v1."
    assert got["versions"][0]["tags"] == ["project:heron"]
    assert got["versions"][0]["actor"] == "Desktop_Agent2"
    assert got["versions"][0]["updated_at"]


def test_versions_are_off_by_default():
    nid = _save("Ibis", "Ibis v1.")
    memory_tools.memory_save(content="Ibis v2.", id=nid, author="Desktop_Agent2")
    got = memory_tools.memory_get(nid)
    assert "versions" not in got and "versions_total" not in got


def test_unchanged_content_and_tags_record_nothing():
    nid = _save("Jackal", "Jackal text.", tags=["fact"])
    # Same content re-saved, metadata-only writes, feedback and reads.
    memory_tools.memory_save(content="Jackal text.", id=nid, author="Desktop_Agent2", salience=0.7)
    memory_tools.memory_feedback(nid, "helpful")
    memory_tools.memory_get(nid)
    memory_tools.memory_search("jackal")
    _sql("UPDATE memory SET title='Jackal 2', access_count=access_count+5 WHERE id=?", (nid,))
    assert _versions(nid) == []


def test_tags_only_change_and_append_both_record_a_version():
    nid = _save("Koala", "Koala text.", tags=["fact"])
    memory_tools.memory_save(content="Koala text.", id=nid, author="Desktop_Agent2", tags=["pref"])
    memory_tools.memory_save(content="More koala.", id=nid, author="Desktop_Agent2", append=True)
    rows = _versions(nid)
    assert [r[0] for r in rows] == [1, 2]
    assert json.loads(rows[0][2]) == ["fact"]
    assert rows[1][1] == "Koala text."


def test_direct_sql_edit_is_captured():
    nid = _save("Lynx", "Lynx original.")
    _sql(
        "UPDATE memory SET content='Lynx edited by hand', updated_by='maintenance' WHERE id=?",
        (nid,),
    )
    rows = _versions(nid)
    assert [(r[0], r[1]) for r in rows] == [(1, "Lynx original.")]


def test_edit_refused_by_a_failed_chunk_delete_records_no_version(monkeypatch):
    """#2184 refuses an edit whose chunk delete fails; its version rolls back with it."""
    nid = _save("Gecko", "Gecko v1.")
    working_delete = storage.delete_memory_chunks

    def broken_delete(conn, note_id):
        raise sqlite3.OperationalError("vec0 mirror delete failed")

    monkeypatch.setattr(storage, "delete_memory_chunks", broken_delete)
    with pytest.raises(ToolError, match="chunk"):
        memory_tools.memory_save(content="Gecko v2.", id=nid, author="Desktop_Agent2")
    assert _sql("SELECT content FROM memory WHERE id=?", (nid,)) == [("Gecko v1.",)]
    assert _versions(nid) == []

    monkeypatch.setattr(storage, "delete_memory_chunks", working_delete)
    memory_tools.memory_save(content="Gecko v2.", id=nid, author="Desktop_Agent2")
    assert [(r[0], r[1]) for r in _versions(nid)] == [(1, "Gecko v1.")]


def test_index_notes_are_not_versioned():
    _save("Marmot", "Marmot one.", tags=["project:marmot"])
    _save("Marmot two", "Marmot two.", tags=["project:marmot"])
    _save("Marmot three", "Marmot three.", tags=["project:marmot"])
    index_ids = [memory_tools._master_index_id(), memory_tools._tag_index_id("project:marmot")]
    assert all(_sql("SELECT 1 FROM memory WHERE id=?", (i,)) for i in index_ids)
    for index_id in index_ids:
        assert _versions(index_id) == []


def test_versions_are_newest_first_and_bounded():
    nid = _save("Newt", "Newt 0.")
    conn = storage.connect()
    try:
        for i in range(1, table_schemas.MEMORY_VERSIONS_KEPT + 11):
            conn.execute("UPDATE memory SET content=? WHERE id=?", (f"Newt {i}.", nid))
        conn.commit()
    finally:
        conn.close()
    stored = _versions(nid)
    # Retention: the trigger keeps the newest MEMORY_VERSIONS_KEPT.
    assert len(stored) == table_schemas.MEMORY_VERSIONS_KEPT
    assert stored[-1][0] == table_schemas.MEMORY_VERSIONS_KEPT + 10
    got = memory_tools.memory_get(nid, include_versions=True)
    shown = got["versions"]
    assert len(shown) == memory_tools.MAX_VERSIONS_SHOWN
    assert got["versions_total"] == table_schemas.MEMORY_VERSIONS_KEPT
    numbers = [v["version"] for v in shown]
    assert numbers == sorted(numbers, reverse=True)
    assert shown[0]["content"] == f"Newt {table_schemas.MEMORY_VERSIONS_KEPT + 9}."


def test_as_of_hides_a_version_captured_after_it():
    """Review P2 repro: the edit happened after as_of, so no version existed then."""
    nid = _save("Okapi", "Okapi v1.")
    _sql(
        "UPDATE memory SET created_at=?, updated_at=? WHERE id=?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", nid),
    )
    memory_tools.memory_save(content="Okapi v2.", id=nid, author="Desktop_Agent2")
    # The captured state was written before as_of, but replaced after it.
    assert _sql("SELECT updated_at FROM memory_versions WHERE note_id=?", (nid,)) == [
        ("2026-01-01T00:00:00+00:00",)
    ]
    got = memory_tools.memory_get(nid, include_versions=True, as_of="2026-02-01T00:00:00+00:00")
    assert got["versions"] == []
    assert got["versions_total"] == 0
    assert memory_tools.memory_get(nid, include_versions=True)["versions_total"] == 1


def test_as_of_shows_only_states_already_replaced_by_then():
    nid = _save("Ostrich", "Ostrich v1.")
    _sql(
        "UPDATE memory SET created_at=?, updated_at=? WHERE id=?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", nid),
    )
    memory_tools.memory_save(content="Ostrich v2.", id=nid, author="Desktop_Agent2")
    # v1 was replaced on 2026-01-15; v2 is replaced now.
    _sql(
        "UPDATE memory_versions SET recorded_at=? WHERE note_id=? AND version=1",
        ("2026-01-15T00:00:00+00:00", nid),
    )
    memory_tools.memory_save(content="Ostrich v3.", id=nid, author="Desktop_Agent2")
    everything = memory_tools.memory_get(nid, include_versions=True)["versions"]
    assert [v["content"] for v in everything] == ["Ostrich v2.", "Ostrich v1."]
    got = memory_tools.memory_get(nid, include_versions=True, as_of="2026-02-01T00:00:00+00:00")
    assert [v["content"] for v in got["versions"]] == ["Ostrich v1."]
    assert got["versions"][0]["recorded_at"] == "2026-01-15T00:00:00+00:00"
    assert got["versions_total"] == 1


def test_recorded_at_is_the_capture_time_in_storage_format():
    nid = _save("Otter", "Otter v1.")
    before = datetime.now(UTC).replace(microsecond=0)
    memory_tools.memory_save(content="Otter v2.", id=nid, author="Desktop_Agent2")
    after = datetime.now(UTC)
    version = memory_tools.memory_get(nid, include_versions=True)["versions"][0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", version["recorded_at"])
    assert len(version["recorded_at"]) == len(storage.now())
    assert before <= datetime.fromisoformat(version["recorded_at"]) <= after


def test_soft_delete_keeps_versions_like_its_tombstone(monkeypatch):
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
    nid = _save("Puffin", "Puffin secret plan.")
    assert memory_tools.memory_delete(nid)["deleted"]
    got = memory_tools.memory_get(nid, include_versions=True)
    assert got["deleted"] is True
    # The tombstone already preserves the text; the version adds no new exposure.
    assert "Puffin secret plan." in got["content"]
    assert got["versions"][0]["content"] == "Puffin secret plan."


def test_hard_delete_erases_versions():
    nid = _save("Quail", "Quail v1 with private detail.")
    memory_tools.memory_save(content="Quail v2.", id=nid, author="Desktop_Agent2")
    assert len(_versions(nid)) == 1
    conn = storage.connect()
    try:
        assert storage.hard_delete_memory(conn, nid, org_id=None)
        conn.commit()
    finally:
        conn.close()
    assert _versions(nid) == []
    assert _sql("SELECT COUNT(*) FROM memory_versions WHERE content LIKE '%private detail%'") == [
        (0,)
    ]


def test_raw_delete_without_foreign_keys_still_erases_versions():
    nid = _save("Raven", "Raven v1.")
    memory_tools.memory_save(content="Raven v2.", id=nid, author="Desktop_Agent2")
    conn = sqlite3.connect(config.db_path())
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM memory WHERE id=?", (nid,))
        conn.commit()
    finally:
        conn.close()
    assert _versions(nid) == []


def test_reused_id_after_hard_delete_starts_clean():
    nid = _save("Stoat", "Stoat v1.")
    memory_tools.memory_save(content="Stoat v2.", id=nid, author="Desktop_Agent2")
    conn = storage.connect()
    try:
        storage.hard_delete_memory(conn, nid, org_id=None)
        stamp = storage.now()
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at) "
            "VALUES (?, 'Stoat', 'New stoat.', '[]', ?, ?)",
            (nid, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()
    assert memory_tools.memory_get(nid, include_versions=True)["versions"] == []


def test_versions_respect_the_tenant_fence(as_org):
    as_org("org_a")
    nid = _save("Tern", "Tern v1 for org a.")
    memory_tools.memory_save(content="Tern v2.", id=nid, author="Desktop_Agent2")
    assert len(memory_tools.memory_get(nid, include_versions=True)["versions"]) == 1
    as_org("org_b")
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_get(nid, include_versions=True)


def test_schema_is_idempotent():
    conn = storage.connect()
    try:
        table_schemas.memory_versions(conn)
        table_schemas.memory_versions(conn)
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='memory' "
            "AND name LIKE 'memory_versions%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    assert [t[0] for t in triggers] == [
        "memory_versions_capture",
        "memory_versions_on_memory_delete",
    ]


def test_trigger_definitions_are_kept_current_without_churn():
    conn = storage.connect()
    try:
        expected = {
            name: table_schemas.stored_trigger_sql(ddl)
            for name, ddl in table_schemas._MEMORY_VERSIONS_TRIGGERS.items()
        }

        def stored() -> dict[str, str]:
            return {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'memory_versions%'"
                )
            }

        # init_db stored exactly the current definitions, so a re-run is a no-op.
        assert stored() == expected
        # A stale definition from an older deploy is replaced.
        conn.execute("DROP TRIGGER memory_versions_capture")
        conn.execute(
            "CREATE TRIGGER memory_versions_capture BEFORE UPDATE OF content ON memory "
            "BEGIN SELECT 1; END"
        )
        table_schemas.memory_versions(conn)
        assert stored() == expected
    finally:
        conn.close()


# --- revisions, browse reads and in-place restore (task #2515) ------------------------


@pytest.fixture
def as_agent():
    tokens = []

    def _set(label: str) -> None:
        tokens.append(config.agent_identity_var.set(label))

    yield _set
    for token in reversed(tokens):
        config.agent_identity_var.reset(token)


def test_revision_moves_with_edits_not_with_reads_or_feedback():
    saved = memory_tools.memory_save(content="v1", title="rev", author="Desktop_Agent2")
    first = memory_tools.memory_get(saved["id"])["revision"]
    assert saved["revision"] == first and re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    memory_tools.memory_feedback(saved["id"], "helpful")
    assert memory_tools.memory_get(saved["id"])["revision"] == first
    updated = memory_tools.memory_save(content="v2", id=saved["id"], author="Desktop_Agent2")
    assert updated["revision"] != first
    assert updated["revision"] == memory_tools.memory_get(saved["id"])["revision"]


def test_browse_read_leaves_access_stats_alone():
    note_id = _save("browse", "body")
    browsed = memory_tools.memory_get(note_id, count_access=False)
    assert browsed["access_count"] == 0 and browsed["last_accessed"] is None
    assert _sql("SELECT access_count FROM memory WHERE id=?", (note_id,)) == [(0,)]
    assert memory_tools.memory_get(note_id)["access_count"] == 1


def test_stale_replace_is_refused_and_keeps_the_peers_append():
    note_id = _save("cas", "base")
    stale = memory_tools.memory_get(note_id)["revision"]
    memory_tools.memory_save(content="peer line", id=note_id, append=True, author="Laptop_Agent5")
    with pytest.raises(ToolError) as refused:
        memory_tools.memory_save(
            content="replace", id=note_id, author="Desktop_Agent2", expected_revision=stale
        )
    current = memory_tools.memory_get(note_id)
    assert refused.value.code == "conflict"
    assert refused.value.data["current_revision"] == current["revision"]
    assert "peer line" in current["content"]
    ok = memory_tools.memory_save(
        content="merged", id=note_id, author="Desktop_Agent2", expected_revision=current["revision"]
    )
    assert memory_tools.memory_get(note_id)["content"] == "merged"
    assert ok["revision"] != current["revision"]


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"expected_revision": "sha256:" + "0" * 64}, "applies to an update"),
        ({"id": "x", "expected_revision": "abc"}, "must be the `revision`"),
    ],
)
def test_expected_revision_input_rules(kwargs, needle):
    with pytest.raises(ToolError, match=needle):
        memory_tools.memory_save(content="c", author="Desktop_Agent2", **kwargs)


@pytest.mark.parametrize("restore", [memory_tools.memory_restore, consolidate.restore_note])
def test_restore_undoes_a_delete_in_place(as_agent, restore):
    as_agent("Desktop_Agent2")
    target = _save("target", "linked to")
    note_id = _save(
        "keepme", "task #2515 body", tags=["project:x", "fact"], links=[f"extends:{target}"]
    )
    assert memory_tools.memory_delete(note_id) == {"deleted": True}
    assert _sql("SELECT COUNT(*) FROM memory_entities WHERE note_id=?", (note_id,)) == [(0,)]
    restored = restore(note_id)
    assert restored["restored"] is True and restored["was_deleted"] is True
    note = memory_tools.memory_get(note_id, include_versions=True)
    assert note["content"] == "task #2515 body" and note["tags"] == ["fact", "project:x"]
    assert "deleted" not in note and note["revision"] == restored["revision"]
    assert [link["id"] for link in note["links"]["out"]] == [target]
    assert any("meta:deleted" in v["tags"] for v in note["versions"])
    assert note_id in [n["id"] for n in memory_tools.memory_list(tag="project:x")["items"]]
    assert _sql("SELECT COUNT(*) FROM memory_entities WHERE note_id=?", (note_id,)) != [(0,)]


@pytest.mark.parametrize("restore", [memory_tools.memory_restore, consolidate.restore_note])
def test_restoring_a_deleted_note_takes_the_delete_rules(as_agent, restore):
    as_agent("Desktop_Agent2")
    note_id = _save("owned", "body")
    memory_tools.memory_delete(note_id)
    as_agent("Laptop_Agent5")
    with pytest.raises(ToolError, match="owner or a verified human"):
        restore(note_id)
    as_agent("Desktop_Agent2")
    _sql("DELETE FROM memory_versions WHERE note_id=?", (note_id,))
    with pytest.raises(ToolError, match="no recorded version from before its deletion"):
        restore(note_id)
    assert memory_tools.memory_get(note_id)["deleted"] is True


@pytest.mark.parametrize("restore", [memory_tools.memory_restore, consolidate.restore_note])
def test_a_human_delete_needs_a_human_to_restore(as_agent, restore):
    """Review of #1532: the owner could undo a verified human's deliberate delete."""
    human = config.operator_label()
    as_agent("Desktop_Agent2")
    note_id = _save("owned", "body")
    as_agent(human)
    memory_tools.memory_delete(note_id)
    as_agent("Desktop_Agent2")
    with pytest.raises(ToolError, match="deleted by a verified human"):
        restore(note_id)
    assert "meta:deleted-by-human" in memory_tools.memory_get(note_id)["tags"]
    # A tombstone from before the mark is judged by the identity that wrote it.
    _sql(
        "UPDATE memory SET tags=? WHERE id=?",
        (json.dumps(["meta:deleted"]), note_id),
    )
    with pytest.raises(ToolError, match="deleted by a verified human"):
        restore(note_id)
    as_agent(human)
    assert restore(note_id)["was_deleted"] is True
    assert memory_tools.memory_get(note_id)["tags"] == []


@pytest.mark.parametrize("restore", [memory_tools.memory_restore, consolidate.restore_note])
def test_deleted_restore_keeps_supersede_until_second_restore(as_agent, restore):
    as_agent("Desktop_Agent2")
    survivor = _save("survivor", "keep")
    note_id = _save("superseded", "retained body")
    consolidate.apply_supersede(survivor, [note_id])
    memory_tools.memory_delete(note_id)

    undeleted = restore(note_id)

    assert undeleted["was_deleted"] is True
    assert undeleted["superseded_by"] == survivor
    assert memory_tools.memory_get(note_id)["superseded_by"] == survivor
    assert restore(note_id) == {
        "id": note_id,
        "restored": True,
        "was_superseded_by": survivor,
    }
    assert restore(note_id) == {"id": note_id, "restored": False, "was_superseded_by": None}


@pytest.mark.parametrize("restore", [memory_tools.memory_restore, consolidate.restore_note])
def test_deleted_restore_hides_other_tenant(as_agent, as_org, restore):
    as_agent("Desktop_Agent2")
    as_org("org_restore_a")
    note_id = _save("private", "retained body")
    memory_tools.memory_delete(note_id)
    as_org("org_restore_b")

    with pytest.raises(ToolError, match="no memory note"):
        restore(note_id)

    as_org("org_restore_a")
    assert memory_tools.memory_get(note_id)["deleted"] is True


def test_cli_reports_deleted_note_restoration(as_agent, monkeypatch, capsys):
    as_agent("Desktop_Agent2")
    note_id = _save("restore cli", "retained body")
    memory_tools.memory_delete(note_id)
    monkeypatch.setattr("sys.argv", ["unified-agent-consolidate", "restore", note_id])

    consolidate.main()

    assert f"Restored {note_id} from deleted version " in capsys.readouterr().out
    assert memory_tools.memory_get(note_id)["content"] == "retained body"


def test_cli_restore_flushes_snapshot_before_return(as_agent, monkeypatch):
    from unified_agent import sync

    as_agent("Desktop_Agent2")
    note_id = _save("restore snapshot", "retained body")
    memory_tools.memory_delete(note_id)
    vault_root = config.vault_root()
    assert vault_root is not None
    vault_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UA_STATE_SYNC", "1")
    monkeypatch.setenv("UA_STATE_SYNC_DEBOUNCE", "60")
    sync.sync_state_to_vault()
    snapshot = (
        vault_root / config.state_sync_dir() / config.state_mirror_name("state_memory", ".txt")
    )
    try:
        restore_note_result = consolidate.restore_note(note_id)

        assert restore_note_result["was_deleted"] is True
        mirrored = next(note for note in json.loads(snapshot.read_text()) if note["id"] == note_id)
        assert mirrored["content"] == "retained body"
        assert "meta:deleted" not in mirrored["tags"]
    finally:
        sync.flush_pending_sync()


def test_revision_covers_links_and_epistemic_fields_within_one_second(monkeypatch):
    """Review of #1532: a link-only change in the same second kept the revision, so a
    stale save erased the link."""
    monkeypatch.setattr(storage, "now", lambda: "2026-11-11T12:00:00+00:00")
    target = _save("target", "t")
    note_id = _save("source", "s")
    stale = memory_tools.memory_get(note_id)["revision"]
    memory_tools.memory_save(
        content="s", id=note_id, author="Desktop_Agent2", links=[f"extends:{target}"]
    )
    linked = memory_tools.memory_get(note_id)["revision"]
    assert linked != stale
    memory_tools.memory_save(
        content="s", id=note_id, author="Desktop_Agent2", evidence=["task:2515"], epistemic="fact"
    )
    assert memory_tools.memory_get(note_id)["revision"] != linked
    with pytest.raises(ToolError) as refused:
        memory_tools.memory_save(
            content="s", id=note_id, author="Desktop_Agent2", links=[], expected_revision=stale
        )
    assert refused.value.code == "conflict"
    assert [link["id"] for link in memory_tools.memory_get(note_id)["links"]["out"]] == [target]
