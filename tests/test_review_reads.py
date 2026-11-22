"""The head a review read happened at (task #2259).

The release gate needs to distinguish "a human approved this head" from
"somebody read this tree". These tests cover the storage that makes the second
question answerable: one row per ``(repo, number, head_sha, kind, reader)`` — so
a second reader at a head never overwrites the first — a ``delta-read`` that
names the head it moved from, and validation strict enough that a gate reading
the table never has to defend against its own rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from unified_agent import review_reads

REPO = "anonb3ll/unified-agent"
HEAD = "a" * 40
OLDER = "b" * 40


def _record(**over: object) -> int:
    # Deliberately untyped values: the refusal tests hand these functions the
    # wrong types on purpose.
    kwargs: dict[str, Any] = dict(
        repo=REPO,
        number=7,
        head_sha=HEAD,
        kind=review_reads.STEWARD_READ,
        reader="Desktop_Agent5",
        verdict="pass",
    )
    kwargs.update(over)
    return review_reads.record(**kwargs)


def test_record_and_read_back_the_head():
    _record(files=["src/unified_agent/server.py"], evidence_url="https://example.invalid/r/1")
    rows = review_reads.for_head(REPO, 7, HEAD)
    assert len(rows) == 1
    row = rows[0]
    assert row["head_sha"] == HEAD
    assert row["kind"] == review_reads.STEWARD_READ
    assert row["reader"] == "Desktop_Agent5"
    assert row["verdict"] == "pass"
    assert row["files"] == ["src/unified_agent/server.py"]
    assert row["from_sha"] is None
    assert row["evidence_url"] == "https://example.invalid/r/1"
    assert row["created_at"]


def test_re_reading_the_same_head_by_one_reader_updates_its_own_row():
    first = _record()
    second = _record(verdict="pass-after-delta")
    assert first == second
    rows = review_reads.for_head(REPO, 7, HEAD)
    assert len(rows) == 1
    assert rows[0]["reader"] == "Desktop_Agent5"
    assert rows[0]["verdict"] == "pass-after-delta"


def test_a_second_reader_at_the_same_head_keeps_the_first_readers_row():
    """Task #2641: an author's later read must not replace an independent one.

    Reproduced on PR #1608, where a steward read (row 209) and a second reader's
    read at the same head collapsed into one row naming only the later reader,
    so the gate could not tell who had actually read the tree.
    """
    first = _record(reader="Desktop_Agent1", verdict="pass")
    second = _record(reader="Desktop_Agent5", verdict="pass")
    assert first != second
    rows = review_reads.for_head(REPO, 7, HEAD)
    assert sorted(r["reader"] for r in rows) == ["Desktop_Agent1", "Desktop_Agent5"]
    assert {r["id"] for r in rows} == {first, second}
    # Newest first, so a gate that reads the top row still sees the last reader.
    assert [r["reader"] for r in rows] == ["Desktop_Agent5", "Desktop_Agent1"]


def test_two_readers_at_one_head_survive_an_ensure_schema_pass():
    _record(reader="Desktop_Agent1", verdict="pass")
    _record(reader="Desktop_Agent5", verdict="pass")
    conn = review_reads.storage.connect()
    try:
        review_reads.ensure_schema(conn)
    finally:
        conn.close()
    assert len(review_reads.for_head(REPO, 7, HEAD)) == 2


def test_ensure_schema_rebuilds_a_table_with_the_old_key():
    """The shipped table keyed a read by (repo, number, head, kind); SQLite
    cannot alter that constraint, so the migration copies every row across."""
    conn = review_reads.storage.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS review_reads")
        conn.execute(
            "CREATE TABLE review_reads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
            "number INTEGER NOT NULL, head_sha TEXT NOT NULL, from_sha TEXT, "
            "kind TEXT NOT NULL, reader TEXT NOT NULL, verdict TEXT NOT NULL, "
            "files_json TEXT NOT NULL DEFAULT '[]', evidence_url TEXT, "
            "created_at TEXT NOT NULL, UNIQUE (repo, number, head_sha, kind))"
        )
        conn.execute(
            "INSERT INTO review_reads (repo, number, head_sha, kind, reader, verdict, "
            "files_json, created_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?)",
            (
                REPO,
                7,
                HEAD,
                review_reads.STEWARD_READ,
                "Desktop_Agent5",
                "pass",
                "2026-11-20T04:00:00+00:00",
            ),
        )
        conn.commit()
        review_reads.ensure_schema(conn)
        conn.commit()
        key_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_reads'"
        ).fetchone()["sql"]
    finally:
        conn.close()
    assert review_reads._unique_key_is_per_reader(key_sql)
    rows = review_reads.for_head(REPO, 7, HEAD)
    assert [(r["reader"], r["verdict"]) for r in rows] == [("Desktop_Agent5", "pass")]
    # The old key would have refused this second row outright.
    _record(reader="Desktop_Agent1", verdict="pass")
    assert len(review_reads.for_head(REPO, 7, HEAD)) == 2


def test_ensure_schema_rebuild_keeps_the_autoincrement_high_water_mark():
    """A rebuild must not reissue the id of a row deleted before the migration.

    Dropping the table deletes its ``sqlite_sequence`` row; without carrying the
    mark over, the next insert reuses the deleted id (task #2641 review).
    """
    conn = review_reads.storage.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS review_reads")
        conn.execute(
            "CREATE TABLE review_reads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
            "number INTEGER NOT NULL, head_sha TEXT NOT NULL, from_sha TEXT, "
            "kind TEXT NOT NULL, reader TEXT NOT NULL, verdict TEXT NOT NULL, "
            "files_json TEXT NOT NULL DEFAULT '[]', evidence_url TEXT, "
            "created_at TEXT NOT NULL, UNIQUE (repo, number, head_sha, kind))"
        )
        conn.execute(
            "INSERT INTO review_reads (repo, number, head_sha, kind, reader, verdict, "
            "files_json, created_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?)",
            (REPO, 7, HEAD, review_reads.STEWARD_READ, "Desktop_Agent5", "pass", "t1"),
        )
        conn.execute(
            "INSERT INTO review_reads (repo, number, head_sha, kind, reader, verdict, "
            "files_json, created_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?)",
            (REPO, 7, OLDER, review_reads.STEWARD_READ, "Desktop_Agent5", "pass", "t2"),
        )
        conn.execute("DELETE FROM review_reads WHERE head_sha = ?", (OLDER,))
        conn.commit()
        review_reads.ensure_schema(conn)
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'review_reads'"
        ).fetchone()["seq"]
    finally:
        conn.close()
    # The deleted row's id was 2; the mark must still sit at 2 so the next id is 3.
    assert seq == 2
    assert _record(reader="Desktop_Agent1") == 3


def test_ensure_schema_rebuild_is_atomic_on_its_own(monkeypatch):
    """A failure mid-rebuild rolls back to the old table, not a half-built one."""
    conn = review_reads.storage.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS review_reads")
        conn.execute(
            "CREATE TABLE review_reads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
            "number INTEGER NOT NULL, head_sha TEXT NOT NULL, from_sha TEXT, "
            "kind TEXT NOT NULL, reader TEXT NOT NULL, verdict TEXT NOT NULL, "
            "files_json TEXT NOT NULL DEFAULT '[]', evidence_url TEXT, "
            "created_at TEXT NOT NULL, UNIQUE (repo, number, head_sha, kind))"
        )
        conn.execute(
            "INSERT INTO review_reads (repo, number, head_sha, kind, reader, verdict, "
            "files_json, created_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?)",
            (REPO, 7, HEAD, review_reads.STEWARD_READ, "Desktop_Agent5", "pass", "t1"),
        )
        conn.commit()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated failure after the rename")

        monkeypatch.setattr(review_reads, "_restore_sequence", boom)
        with pytest.raises(RuntimeError, match="simulated failure"):
            review_reads.ensure_schema(conn)
        conn.commit()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_reads'"
        ).fetchone()["sql"]
        count = conn.execute("SELECT COUNT(*) AS n FROM review_reads").fetchone()["n"]
    finally:
        conn.close()
    # The old-key table and its row survived; the rebuild left no trace.
    assert not review_reads._unique_key_is_per_reader(sql)
    assert count == 1


def test_two_kinds_at_one_head_are_two_rows():
    _record()
    _record(kind=review_reads.HUMAN, reader="Desktop_Person", verdict="approved")
    rows = review_reads.for_head(REPO, 7, HEAD)
    assert {r["kind"] for r in rows} == {review_reads.STEWARD_READ, review_reads.HUMAN}


def test_a_delta_read_names_the_head_it_moved_from():
    _record(head_sha=OLDER)
    _record(kind=review_reads.DELTA_READ, from_sha=OLDER, files=["deploy/x.sh"])
    row = review_reads.for_head(REPO, 7, HEAD)[0]
    assert row["kind"] == review_reads.DELTA_READ
    assert row["from_sha"] == OLDER


def test_a_delta_read_needs_a_full_read_recorded_at_its_base():
    """A delta read vouches only for what changed since ``from_sha``; with no
    full read at that base the tree as a whole was never read (task #2294)."""
    other = "c" * 40

    def delta(**over: object) -> int:
        return _record(kind=review_reads.DELTA_READ, from_sha=OLDER, files=["a.py"], **over)

    with pytest.raises(ValueError, match="no full read is recorded"):
        delta()
    # A full read of the same head on another pull request is not this one's.
    _record(number=8, head_sha=OLDER)
    with pytest.raises(ValueError, match="no full read is recorded"):
        delta()
    # A delta read is not a base for another delta: record it from the full read.
    _record(head_sha=other)
    _record(head_sha=OLDER, kind=review_reads.DELTA_READ, from_sha=other)
    with pytest.raises(ValueError, match="no full read is recorded"):
        delta()
    assert [r["kind"] for r in review_reads.for_head(REPO, 7, HEAD)] == []
    for kind in review_reads.FULL_READ_KINDS:
        _record(head_sha=OLDER, kind=kind)
        assert delta() == delta()  # idempotent per (repo, number, head, kind)


def test_record_best_effort_surfaces_a_refusal_instead_of_raising():
    out = review_reads.record_best_effort(
        repo=REPO,
        number=7,
        head_sha=HEAD,
        kind=review_reads.DELTA_READ,
        reader="reviewer",
        verdict="pass",
        from_sha=OLDER,
    )
    assert out["recorded"] is None and "no full read is recorded" in out["error"]
    assert review_reads.record_best_effort(
        repo=REPO, number=7, head_sha=HEAD, kind=review_reads.POLICY, reader="r", verdict="ok"
    ) == {"recorded": 1, "error": None}


@pytest.mark.parametrize(
    "over, message",
    [
        ({"repo": "not-a-repo"}, "owner/name"),
        ({"number": 0}, "positive integer"),
        ({"head_sha": "abc"}, "40 lowercase hex"),
        ({"head_sha": "A" * 40}, "40 lowercase hex"),
        ({"kind": "vibes"}, "kind must be one of"),
        ({"reader": "   "}, "reader must be a non-empty string"),
        # A delta read without a base cannot be validated, and a non-delta row
        # carrying from_sha would read as a delta it is not.
        ({"kind": review_reads.DELTA_READ}, "must name the from_sha"),
        ({"kind": review_reads.DELTA_READ, "from_sha": HEAD}, "two different heads"),
        ({"from_sha": OLDER}, "only a delta-read row carries from_sha"),
    ],
)
def test_malformed_rows_are_refused(over, message):
    with pytest.raises(ValueError, match=message):
        _record(**over)
    assert review_reads.for_pr(REPO, 7) == []


def test_for_pr_returns_every_head_newest_first():
    _record(head_sha=OLDER)
    _record(head_sha=HEAD)
    rows = review_reads.for_pr(REPO, 7)
    assert [r["head_sha"] for r in rows] == [HEAD, OLDER]
    assert review_reads.for_pr(REPO, 7, limit=1) == rows[:1]
    assert review_reads.for_pr(REPO, 8) == []


def _seed_heads(count: int) -> None:
    for i in range(count):
        _record(head_sha=f"{i:040x}")


def test_for_pr_has_no_implicit_row_cap():
    """A gate asks "was this tree read?" and must not be answered by a paging
    window: an old reviewed ancestor beyond the default would read as absent."""
    _seed_heads(60)
    rows = review_reads.for_pr(REPO, 7)
    assert len(rows) == 60
    assert review_reads.count_for_pr(REPO, 7) == 60


def test_for_pr_pages_without_losing_or_repeating_a_row():
    _seed_heads(60)
    everything = [r["id"] for r in review_reads.for_pr(REPO, 7)]
    walked: list[int] = []
    after_id = None
    while True:
        page = review_reads.for_pr(REPO, 7, limit=7, after_id=after_id)
        if not page:
            break
        walked.extend(r["id"] for r in page)
        after_id = page[-1]["id"]
    assert walked == everything
    assert len(walked) == review_reads.count_for_pr(REPO, 7) == 60


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"limit": 0}, "limit must be a positive integer"),
        ({"limit": -3}, "limit must be a positive integer"),
        ({"after_id": 0}, "after_id must be a positive integer"),
    ],
)
def test_for_pr_refuses_a_malformed_window(kwargs, message):
    with pytest.raises(ValueError, match=message):
        review_reads.for_pr(REPO, 7, **kwargs)


def test_for_pr_rejects_a_cursor_from_another_pull_request():
    other = _record(number=8)
    with pytest.raises(ValueError, match="already returned for this pull request"):
        review_reads.for_pr(REPO, 7, limit=5, after_id=other)


def test_repeated_paths_collapse_to_one():
    _record(files=["deploy/x.sh", "deploy/x.sh", "src/a.py"])
    assert review_reads.for_head(REPO, 7, HEAD)[0]["files"] == ["deploy/x.sh", "src/a.py"]


@pytest.mark.parametrize(
    "files, message",
    [
        ("deploy/x.sh", "files must be a list of changed paths"),
        ([""], "each files entry must be a non-empty string"),
        (["   "], "each files entry must be a non-empty string"),
        ([42], "each files entry must be a non-empty string"),
        (["x" * 501], "each files entry must be a non-empty string"),
        ([f"f{i}.py" for i in range(2001)], "files may list at most 2000 paths"),
    ],
)
def test_a_malformed_file_list_is_refused(files, message):
    with pytest.raises(ValueError, match=message):
        _record(files=files)
    assert review_reads.for_pr(REPO, 7) == []
