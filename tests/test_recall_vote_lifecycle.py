"""Delayed vote bookkeeping must not recreate a hard-deleted note's votes (#1885)."""

import pytest

from unified_agent import edition, recall_curation, storage


def seed(note_id, *, org_id=None):
    conn = storage.connect()
    try:
        stamp = storage.now()
        conn.execute(
            "INSERT INTO memory(id,content,created_at,updated_at,org_id) VALUES (?, 'fixture',?,?,?)",
            (note_id, stamp, stamp, org_id),
        )
        conn.commit()
    finally:
        conn.close()


def vote_row(note_id):
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT recalled_count,upvoted_count FROM memory_recall_votes WHERE note_id=?",
            (note_id,),
        ).fetchone()
    finally:
        conn.close()


@pytest.mark.parametrize("writer", ["recall", "upvote"])
def test_delayed_vote_does_not_recreate_votes_after_committed_delete(writer):
    seed("deleted")
    captured_ids = ["deleted"]  # Search/feedback observed the note before deletion.
    deleter = storage.connect()
    try:
        # The postcondition of #1815 cleanup, on a separate connection. This
        # regression does not depend on that unmerged helper or change its PR.
        deleter.execute("DELETE FROM memory_recall_votes WHERE note_id='deleted'")
        deleter.execute("DELETE FROM memory WHERE id='deleted'")
        deleter.commit()
    finally:
        deleter.close()
    if writer == "recall":
        recall_curation.record_recall_hits(captured_ids, "captured result", 0.75)
    else:
        recall_curation.record_upvote(captured_ids[0])
    assert vote_row("deleted") is None
    if writer == "recall":
        conn = storage.connect()
        try:
            row = conn.execute("SELECT * FROM recall_quality_log").fetchone()
            assert (row["query"], row["result_count"], row["hit"], row["top_score"]) == (
                "captured result",
                1,
                1,
                0.75,
            )
        finally:
            conn.close()


@pytest.mark.parametrize("org_id", [None, "org_a"])
def test_valid_note_vote_increments_remain_unchanged(monkeypatch, org_id):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
    seed("valid", org_id=org_id)
    recall_curation.record_recall_hits(["valid", "valid"], "duplicates retained", 0.5)
    recall_curation.record_upvote("valid")
    recall_curation.record_upvote("valid")
    assert tuple(vote_row("valid")) == (2, 2)


@pytest.mark.parametrize("existing_vote", [False, True])
def test_foreign_note_votes_cannot_be_created_or_incremented(monkeypatch, existing_vote):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    seed("foreign", org_id="org_b")
    if existing_vote:
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO memory_recall_votes(note_id,recalled_count,upvoted_count) "
                "VALUES ('foreign',3,4)"
            )
            conn.commit()
        finally:
            conn.close()
    recall_curation.record_recall_hits(["foreign"], "stale captured id", 0.5)
    recall_curation.record_upvote("foreign")
    row = vote_row("foreign")
    assert (tuple(row) if row else None) == ((3, 4) if existing_vote else None)


def test_mixed_recall_batch_keeps_valid_votes_and_query_quality():
    seed("valid")
    seed("foreign", org_id="other")
    recall_curation.record_recall_hits(["valid", "missing", "foreign"], "mixed", 0.8)
    assert tuple(vote_row("valid")) == (1, 0)
    assert vote_row("missing") is None
    assert vote_row("foreign") is None
    conn = storage.connect()
    try:
        row = conn.execute("SELECT result_count,hit FROM recall_quality_log").fetchone()
        assert tuple(row) == (3, 1)
    finally:
        conn.close()


def test_search_side_writes_share_one_commit(monkeypatch):
    """memory_search bumps access stats and records recall votes and the
    quality log on one connection with one commit (task #2444)."""
    from unified_agent import embeddings
    from unified_agent.tools.memory import memory_save, memory_search

    monkeypatch.setattr(embeddings, "generate_embedding", lambda _t: 1 / 0)
    note = memory_save(content="quokka habitat notes", title="Quokka", author="Desktop_Agent1")
    opened: list = []
    real_connect = storage.connect

    def counting_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(storage, "connect", counting_connect)
    res = memory_search("quokka")
    assert [r["id"] for r in res["results"]] == [note["id"]]
    assert res["results"][0]["access_count"] == 1
    # One read connection, one write connection: no third for the recall log.
    assert len(opened) == 2
    monkeypatch.setattr(storage, "connect", real_connect)
    assert vote_row(note["id"])["recalled_count"] == 1
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT access_count FROM memory WHERE id=?", (note["id"],)).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM recall_quality_log").fetchone()[0] == 1
    finally:
        conn.close()


def test_failed_recall_write_keeps_the_access_bump(monkeypatch):
    """A recall write that fails rolls back only to its savepoint."""
    seed("kept")
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET access_count = access_count + 1 WHERE id='kept'")

        def broken(*_a, **_kw):
            raise RuntimeError("log write failed")

        monkeypatch.setattr(recall_curation, "_trim_quality_log", broken)
        recall_curation.record_recall_hits(["kept"], "q", 0.5, conn=conn)
        conn.commit()
    finally:
        conn.close()
    assert vote_row("kept") is None  # the vote rolled back with the failed log write
    conn = storage.connect()
    try:
        assert conn.execute("SELECT access_count FROM memory WHERE id='kept'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM recall_quality_log").fetchone()[0] == 0
    finally:
        conn.close()


def _recall_rows() -> tuple[object, int]:
    conn = storage.connect()
    try:
        return vote_row("kept"), conn.execute("SELECT COUNT(*) FROM recall_quality_log").fetchone()[
            0
        ]
    finally:
        conn.close()


def test_idle_caller_connection_is_not_committed():
    """Task #2487: on a connection with no open transaction the helper's
    savepoint was the outermost one, so releasing it committed the hits and
    the caller's rollback could not undo them."""
    seed("kept")
    conn = storage.connect()
    try:
        assert not conn.in_transaction
        recall_curation.record_recall_hits(["kept"], "q", 0.5, conn=conn)
        assert conn.in_transaction  # the writes wait for the caller's decision
        conn.rollback()
        assert _recall_rows() == (None, 0)
        recall_curation.record_recall_hits(["kept"], "q", 0.5, conn=conn)
        conn.commit()
    finally:
        conn.close()
    votes, logged = _recall_rows()
    assert votes["recalled_count"] == 1 and logged == 1


def test_failed_recall_write_on_an_idle_connection_leaves_it_idle(monkeypatch):
    seed("kept")

    def broken(*_a, **_kw):
        raise RuntimeError("log write failed")

    monkeypatch.setattr(recall_curation, "_trim_quality_log", broken)
    conn = storage.connect()
    try:
        recall_curation.record_recall_hits(["kept"], "q", 0.5, conn=conn)
        assert not conn.in_transaction  # nothing of the caller's was open to keep
        conn.commit()
    finally:
        conn.close()
    assert _recall_rows() == (None, 0)


def test_search_without_hits_still_commits_its_quality_row(monkeypatch):
    """memory_search hands over an idle connection when nothing matched; it
    commits after the helper, so the miss is still logged (task #2487)."""
    from unified_agent import embeddings
    from unified_agent.tools.memory import memory_search

    monkeypatch.setattr(embeddings, "generate_embedding", lambda _t: 1 / 0)
    assert memory_search("nothing stored matches this")["results"] == []
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT result_count,hit FROM recall_quality_log").fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in rows] == [(0, 0)]
