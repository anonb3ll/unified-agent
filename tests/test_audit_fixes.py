"""Audit fixes 3-5 (tasks #163, #164, #165).

Each covers a place where the work was done and then thrown away:
  #163 cosine similarity scored notes that recall already hides,
  #164 compact task listings read every notes body only to drop it,
  #165 the vault write-lock guard read a whole note to inspect its first lines.
"""

import os
from pathlib import Path

import pytest

from unified_agent import storage
from unified_agent.embeddings import (
    cosine_similarity_bytes,
    embedding_to_bytes,
    generate_embedding,
)
from unified_agent.tools import vault as vault_mod
from unified_agent.tools.memory import _semantic_similarity_rows, memory_save
from unified_agent.tools.tasks import task_add, task_claim, task_list, task_update


class TestSupersededPreFilter:
    """#163 — superseded notes must not reach the similarity scan."""

    def _supersede(self, note_id: str, by: str) -> None:
        conn = storage.connect()
        try:
            conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (by, note_id))
            conn.commit()
        finally:
            conn.close()

    @pytest.mark.embeddings
    def test_superseded_notes_are_excluded_from_similarity_rows(self, isolated_env):
        live = memory_save("git worktrees check out two branches at once", title="Live")
        dead = memory_save("git worktrees check out two branches at once", title="Dead")
        self._supersede(dead["id"], live["id"])

        conn = storage.connect()
        try:
            q = embedding_to_bytes(generate_embedding("git worktrees"))
            ids = {r["note_id"] for r in _semantic_similarity_rows(conn, q)}
        finally:
            conn.close()

        assert live["id"] in ids
        assert dead["id"] not in ids

    @pytest.mark.embeddings
    def test_opting_out_still_returns_superseded_notes(self, isolated_env):
        live = memory_save("kubernetes ingress routing rules", title="Live")
        dead = memory_save("kubernetes ingress routing rules", title="Dead")
        self._supersede(dead["id"], live["id"])

        conn = storage.connect()
        try:
            q = embedding_to_bytes(generate_embedding("kubernetes ingress"))
            ids = {
                r["note_id"] for r in _semantic_similarity_rows(conn, q, exclude_superseded=False)
            }
        finally:
            conn.close()

        assert {live["id"], dead["id"]} <= ids

    @pytest.mark.embeddings
    def test_index_and_superseded_filters_compose(self, isolated_env):
        live = memory_save("terraform state locking with dynamodb", title="Live")
        dead = memory_save("terraform state locking with dynamodb", title="Dead")
        self._supersede(dead["id"], live["id"])

        conn = storage.connect()
        try:
            q = embedding_to_bytes(generate_embedding("terraform state"))
            rows = _semantic_similarity_rows(conn, q, exclude_index=True)
            ids = {r["note_id"] for r in rows}
        finally:
            conn.close()

        assert live["id"] in ids
        assert dead["id"] not in ids
        assert not any(i == "masterindex00" or i.startswith("tagindex_") for i in ids)

    def test_memory_save_does_not_dedup_against_a_superseded_note(self, isolated_env):
        """A note already pointed past must not block saving its replacement."""
        original = memory_save("redis eviction policy allkeys-lru", title="Original")
        self._supersede(original["id"], "some-newer-note")

        res = memory_save("redis eviction policy allkeys-lru", title="Replacement", check_only=True)

        assert all(s["id"] != original["id"] for s in res["similar"])


@pytest.mark.embeddings
class TestQueryVectorMemo:
    """#163 deliverable 2 — the query side of a scan is decoded once, not per row."""

    def test_memoized_result_matches_a_cold_computation(self):
        a = embedding_to_bytes(generate_embedding("first sentence about caching"))
        b = embedding_to_bytes(generate_embedding("second sentence about vectors"))

        import unified_agent.vector_math as emb  # owns the memo since #1720

        emb._query_vec_cache = None
        cold = cosine_similarity_bytes(a, b)
        warm = cosine_similarity_bytes(a, b)

        assert cold == warm
        assert emb._query_vec_cache is not None

    def test_switching_query_vectors_recomputes(self):
        import unified_agent.vector_math as emb  # owns the memo since #1720

        row = embedding_to_bytes(generate_embedding("a candidate row"))
        q1 = embedding_to_bytes(generate_embedding("query one"))
        q2 = embedding_to_bytes(generate_embedding("an entirely different query two"))

        emb._query_vec_cache = None
        s1 = cosine_similarity_bytes(row, q1)
        s2 = cosine_similarity_bytes(row, q2)
        # The memo is keyed on the blob, so a changed query must not reuse it.
        assert s1 != s2
        assert cosine_similarity_bytes(row, q1) == pytest.approx(s1)

    def test_zero_norm_query_still_returns_zero(self):
        import unified_agent.vector_math as emb  # owns the memo since #1720

        emb._query_vec_cache = None
        row = embedding_to_bytes(generate_embedding("a candidate row"))
        zero = embedding_to_bytes([0.0] * 384)
        assert cosine_similarity_bytes(row, zero) == 0.0


class TestCompactListingProjection:
    """#164 — compact mode must not read notes bodies out of SQLite."""

    def test_compact_never_selects_the_notes_column(self, isolated_env, monkeypatch):
        task_add("projected task", notes="x" * 5000 + "\n\nclaimed-by: Desktop_Agent1")

        seen: list[str] = []
        real_connect = storage.connect

        def spy_connect():
            conn = real_connect()
            conn.set_trace_callback(seen.append)
            return conn

        monkeypatch.setattr(storage, "connect", spy_connect)
        task_list(status="open", fields="compact")

        selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
        against_tasks = [s for s in selects if "FROM tasks" in s]
        assert against_tasks, f"expected a SELECT against tasks, saw {selects}"
        assert not any("SELECT *" in s for s in against_tasks), (
            f"compact mode still fetched every column: {against_tasks}"
        )
        # And specifically: `notes` is never selected as a bare column. It may
        # still appear inside LENGTH()/instr()/substr(), which read the value
        # without shipping the body back to Python.
        for sql in against_tasks:
            projection = sql.split("FROM tasks")[0].removeprefix("SELECT")
            bare = [c.strip() for c in projection.split(",")]
            assert "notes" not in bare, f"compact projection selects the notes body: {sql}"

    def test_compact_reports_the_same_values_as_full(self, isolated_env):
        notes = "a delegation spec\n" * 200 + "\nclaimed-by: Desktop_Agent5"
        task = task_add("parity task", notes=notes)
        task_claim(task["id"], agent="Desktop_Agent5")

        full = task_list(status="doing", fields="full")["tasks"][0]
        compact = task_list(status="doing", fields="compact")["tasks"][0]

        assert "notes" not in compact
        assert compact["notes_chars"] == len(full["notes"])
        assert compact["claimed_by"] == "Desktop_Agent5"
        for key in ("id", "title", "status", "priority", "created_at", "worktree_path"):
            assert compact[key] == full[key]

    def test_claim_line_is_found_far_into_a_long_body(self, isolated_env):
        """The SQL window must start at the marker, not at the top of the note."""
        notes = "filler line that is not a claim\n" * 400 + "claimed-by: Desktop_agent3\ntrailer"
        task = task_add("deep claim", notes=notes)
        task_claim(task["id"], agent="Desktop_agent3")

        compact = task_list(status="doing", fields="compact")["tasks"][0]

        assert compact["claimed_by"] == "Desktop_agent3"
        assert compact["notes_chars"] == len(notes)

    def test_unclaimed_task_reports_no_claimant(self, isolated_env):
        task_add("unclaimed", notes="a spec with no claim line at all")
        compact = task_list(status="open", fields="compact")["tasks"][0]
        assert compact["claimed_by"] is None

    def test_empty_notes_compacts_to_zero(self, isolated_env):
        task_add("no notes")
        compact = task_list(status="open", fields="compact")["tasks"][0]
        assert compact["notes_chars"] == 0
        assert compact["claimed_by"] is None

    def test_claimed_by_column_outranks_the_notes_line(self, isolated_env):
        """The DB column is authoritative; the note line is descriptive only."""
        res = task_add("column wins", notes="claimed-by: Notes_Label")
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", ("Column_Label", res["id"]))
            conn.commit()
        finally:
            conn.close()

        compact = task_list(status="open", fields="compact")["tasks"][0]
        assert compact["claimed_by"] == "Column_Label"

    def test_compact_still_honours_the_notes_contain_filter(self, isolated_env):
        task_add("has marker", notes="[waiting-for: review]")
        task_add("no marker", notes="something else")

        res = task_list(status="open", notes_contain="[waiting-for: review]", fields="compact")

        assert [t["title"] for t in res["tasks"]] == ["has marker"]

    def test_status_and_priority_filters_survive_projection(self, isolated_env):
        a = task_add("high open", priority="high")
        task_add("low open", priority="low")
        task_update(a["id"], status="doing")

        res = task_list(status="doing", fields="compact")
        assert [t["id"] for t in res["tasks"]] == [a["id"]]


class TestWaitingForReviewProjection:
    """#1038-1040/#1041 — the compact `waiting_for_review` flag must reflect

    whether a task is *currently* awaiting review, not merely whether the
    marker text is somewhere in its notes.
    """

    def _flag(self, task_id: int, status: str = "all") -> bool:
        rows = task_list(status=status, fields="compact")["tasks"]
        (row,) = [t for t in rows if t["id"] == task_id]
        return row["waiting_for_review"]

    def test_true_while_open_or_doing(self, isolated_env):
        task = task_add("under review", notes="[waiting-for: review] please check")
        assert self._flag(task["id"]) is True

        task_update(task["id"], status="doing")
        assert self._flag(task["id"]) is True

    def test_false_once_done(self, isolated_env):
        """The marker is appended once and never retracted, so a task
        that shipped and was closed must stop reporting waiting_for_review.

        `task_update` itself now refuses to mark a task done while the review
        marker is live and unresolved (`_completion_review_error`) — but the
        ~30 live rows this bug was found against predate that guard (or were
        closed by another path), so simulate the historical state directly
        rather than through `task_update`.
        """
        task = task_add("shipped", notes="[waiting-for: review] please check")
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))
            conn.commit()
        finally:
            conn.close()

        assert self._flag(task["id"]) is False

    def test_false_once_cancelled(self, isolated_env):
        task = task_add("dropped", notes="[waiting-for: review] please check")
        task_update(task["id"], status="cancelled")

        assert self._flag(task["id"]) is False

    def test_false_for_forward_looking_once_shipped_phrasing(self, isolated_env):
        """#1038 — "once shipped" describes a future condition on an
        unstarted task, not a live awaiting-review state."""
        task = task_add(
            "not started",
            notes="[waiting-for: review] once shipped -- CI green + local suite per standing gate.",
        )
        assert self._flag(task["id"]) is False

    def test_false_for_forward_looking_when_done_phrasing(self, isolated_env):
        task = task_add(
            "also not started",
            notes="[waiting-for: review] when done, ping for review.",
        )
        assert self._flag(task["id"]) is False

    def test_true_when_a_live_marker_follows_a_forward_looking_one(self, isolated_env):
        """A forward-looking marker must not mask a later genuine one.

        The natural lifecycle appends: the task is filed saying review is
        needed "once shipped", then the executor appends a bare marker when
        it actually finishes. Inspecting only the qualifier at the FIRST
        marker's offset would report this task as not-live and silently drop
        it out of the review backlog -- a false negative strictly worse than
        the false positive this projection was fixed for.
        """
        task = task_add(
            "filed then shipped",
            notes=(
                "[waiting-for: review] once shipped -- CI green per standing gate.\n\n"
                "[waiting-for: review]"
            ),
        )
        assert self._flag(task["id"]) is True

    def test_true_when_forward_looking_marker_follows_a_live_one(self, isolated_env):
        """Order must not matter -- a live marker anywhere wins."""
        task = task_add(
            "live first",
            notes=("[waiting-for: review]\n\n[waiting-for: review] when done, ping again."),
        )
        assert self._flag(task["id"]) is True


class TestFrontmatterReadAndCache:
    """#165 — the lock guard reads a bounded head and caches the parse."""

    def _note(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "note.md"
        p.write_text(body)
        return p

    def test_locked_note_detected_without_reading_the_whole_file(self, tmp_path, monkeypatch):
        p = self._note(tmp_path, "---\nlocked: true\n---\n" + "filler\n" * 50000)
        vault_mod._frontmatter_cache.clear()

        reads: list[int] = []
        real_open = open

        def is_note(file) -> bool:
            # note_is_locked opens a non-blocking fd and wraps it (#2212), so
            # match an int fd by file identity as well as a path argument.
            if isinstance(file, int):
                return os.path.samestat(os.fstat(file), p.stat())
            return Path(str(file)) == p

        def counting_open(file, mode="r", *a, **k):
            fh = real_open(file, mode, *a, **k)
            if "b" in mode and is_note(file):
                real_read = fh.read

                def read(n=-1):
                    reads.append(n)
                    return real_read(n)

                fh.read = read
            return fh

        monkeypatch.setattr("builtins.open", counting_open)
        assert vault_mod._is_locked_path(p) is True
        assert reads and all(n == vault_mod._FRONTMATTER_MAX_BYTES for n in reads), (
            f"expected a bounded head read, got {reads}"
        )

    def test_unlocked_note_reports_false(self, tmp_path):
        p = self._note(tmp_path, "---\ntitle: Plain\n---\nbody\n")
        vault_mod._frontmatter_cache.clear()
        assert vault_mod._is_locked_path(p) is False

    def test_note_without_frontmatter_reports_false(self, tmp_path):
        p = self._note(tmp_path, "just a body, no frontmatter\n")
        vault_mod._frontmatter_cache.clear()
        assert vault_mod._is_locked_path(p) is False

    def test_cache_is_reused_for_an_unchanged_file(self, tmp_path):
        p = self._note(tmp_path, "---\nlocked: true\n---\nbody\n")
        vault_mod._frontmatter_cache.clear()

        assert vault_mod._is_locked_path(p) is True
        assert str(p) in vault_mod._frontmatter_cache

        # Delete the file: a second call served from cache must not need it.
        p.unlink()
        p.write_text("---\nlocked: true\n---\nbody\n")  # same size, restored
        assert vault_mod._is_locked_path(p) is True

    def test_cache_invalidates_when_the_note_changes(self, tmp_path):
        p = self._note(tmp_path, "---\nlocked: true\n---\nbody\n")
        vault_mod._frontmatter_cache.clear()
        assert vault_mod._is_locked_path(p) is True

        p.write_text("---\nlocked: false\n---\na different body entirely\n")
        assert vault_mod._is_locked_path(p) is False

    def test_frontmatter_past_the_head_bound_is_still_seen(self, tmp_path):
        """A guard we merely failed to read must not report as absent."""
        padding = "# " + "x" * (vault_mod._FRONTMATTER_MAX_BYTES + 2000) + "\n"
        p = self._note(tmp_path, "---\n" + padding + "locked: true\n---\nbody\n")
        vault_mod._frontmatter_cache.clear()

        assert vault_mod._is_locked_path(p) is True

    def test_missing_file_reports_false_rather_than_raising(self, tmp_path):
        vault_mod._frontmatter_cache.clear()
        assert vault_mod._is_locked_path(tmp_path / "nope.md") is False

    def test_text_helper_still_works_for_in_memory_content(self):
        assert vault_mod._is_locked("---\nlocked: true\n---\nbody") is True
        assert vault_mod._is_locked("---\ntitle: x\n---\nbody") is False
        assert vault_mod._is_locked("no frontmatter") is False
