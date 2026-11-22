"""Tests for scripts/bench_memory_search_scale.py (task #967).

The pure numeric helpers (cosine_sims_against_matrix, brute_force_topk,
recall_at_k, percentiles_ms) are the load-bearing recall-ground-truth logic
the benchmark report's numbers depend on — a bug there invalidates the whole
report, so they get direct unit coverage against hand-computed expectations,
no DB or model involved.

embed_missing_notes, insert_synthetic_batch, and db_footprint are thin I/O
glue around real production functions; they get one lightweight integration
test each (marked `embeddings` where a real embedding call is involved) to
prove they wire together correctly, not exhaustive coverage of glue code.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import bench_memory_search as latency_bench  # noqa: E402
import bench_memory_search_scale as bench  # noqa: E402


class TestCosineSimsAgainstMatrix:
    def test_matches_hand_computed_similarity(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array(
            [
                [1.0, 0.0],  # identical direction -> 1.0
                [0.0, 1.0],  # orthogonal -> 0.0
                [-1.0, 0.0],  # opposite -> -1.0
                [1.0, 1.0],  # 45 degrees -> sqrt(2)/2
            ],
            dtype=np.float32,
        )
        sims = bench.cosine_sims_against_matrix(query, matrix)
        expected = np.array([1.0, 0.0, -1.0, 0.70710678], dtype=np.float32)
        np.testing.assert_allclose(sims, expected, atol=1e-5)

    def test_zero_norm_row_scores_zero(self):
        query = np.array([1.0, 2.0], dtype=np.float32)
        matrix = np.array([[0.0, 0.0], [1.0, 2.0]], dtype=np.float32)
        sims = bench.cosine_sims_against_matrix(query, matrix)
        assert sims[0] == 0.0
        assert sims[1] == pytest.approx(1.0, abs=1e-5)

    def test_zero_norm_query_scores_all_zero(self):
        query = np.array([0.0, 0.0], dtype=np.float32)
        matrix = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        sims = bench.cosine_sims_against_matrix(query, matrix)
        assert list(sims) == [0.0, 0.0]

    def test_empty_matrix_returns_empty(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.zeros((0, 0), dtype=np.float32)
        sims = bench.cosine_sims_against_matrix(query, matrix)
        assert sims.shape == (0,)


class TestBruteForceTopK:
    def test_orders_by_descending_similarity(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        ids = ["a", "b", "c"]
        matrix = np.array(
            [
                [0.0, 1.0],  # b-ish: orthogonal, sim 0
                [1.0, 0.0],  # identical, sim 1
                [0.9, 0.1],  # close, sim ~0.994
            ],
            dtype=np.float32,
        )
        top = bench.brute_force_topk(query, ids, matrix, k=2)
        assert [note_id for note_id, _ in top] == ["b", "c"]
        assert top[0][1] == pytest.approx(1.0, abs=1e-5)

    def test_k_larger_than_corpus_returns_everything(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        ids = ["a", "b"]
        matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        top = bench.brute_force_topk(query, ids, matrix, k=10)
        assert len(top) == 2

    def test_empty_corpus_returns_empty(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        top = bench.brute_force_topk(query, [], np.zeros((0, 0), dtype=np.float32), k=5)
        assert top == []


class TestRecallAtK:
    def test_full_overlap_is_one(self):
        assert bench.recall_at_k(["a", "b", "c"], ["c", "b", "a"]) == 1.0

    def test_no_overlap_is_zero(self):
        assert bench.recall_at_k(["a", "b"], ["x", "y"]) == 0.0

    def test_partial_overlap(self):
        assert bench.recall_at_k(["a", "b", "c", "d"], ["a", "c", "z", "y"]) == 0.5

    def test_empty_ground_truth_is_one(self):
        assert bench.recall_at_k([], ["a", "b"]) == 1.0

    def test_extra_returned_ids_do_not_inflate_score(self):
        # returned_ids longer than ground truth must not exceed 1.0
        assert bench.recall_at_k(["a"], ["a", "b", "c", "d"]) == 1.0


class TestPercentilesMs:
    def test_empty_input(self):
        result = bench.percentiles_ms([])
        assert result["n"] == 0
        assert result["p50_ms"] is None

    def test_known_values_in_seconds_converted_to_ms(self):
        # 10 values 0.001s..0.010s -> 1ms..10ms
        latencies_s = [i / 1000.0 for i in range(1, 11)]
        result = bench.percentiles_ms(latencies_s)
        assert result["n"] == 10
        assert result["max_ms"] == pytest.approx(10.0, abs=1e-6)
        assert result["mean_ms"] == pytest.approx(5.5, abs=1e-6)
        # p50 of 1..10 (linear interpolation) is 5.5
        assert result["p50_ms"] == pytest.approx(5.5, abs=1e-6)


class TestSyntheticNote:
    def test_deterministic_given_same_seed(self):
        import random

        rng1 = random.Random(7)
        rng2 = random.Random(7)
        note1 = bench.synthetic_note(rng1, 42)
        note2 = bench.synthetic_note(rng2, 42)
        assert note1 == note2

    def test_varies_across_indices(self):
        import random

        rng = random.Random(7)
        notes = [bench.synthetic_note(rng, i) for i in range(20)]
        contents = {content for _, content, _ in notes}
        assert len(contents) > 1

    def test_content_length_within_bounds(self):
        import random

        rng = random.Random(11)
        for i in range(50):
            _title, content, _tags = bench.synthetic_note(rng, i)
            assert 40 <= len(content) <= 4200  # a little slack past the 4000 clamp target

    def test_tags_are_nonempty_and_topic_scoped(self):
        import random

        rng = random.Random(3)
        _title, _content, tags = bench.synthetic_note(rng, 0)
        assert tags
        assert tags[0].startswith("topic:")


@pytest.fixture
def bench_conn(isolated_env, tmp_path):
    """A real, initialized memory.db connection under an isolated UA_HOME —
    reuses the autouse isolated_env fixture's UA_HOME rather than
    bench.run_benchmark's own isolation setup, since these tests want direct
    control over the connection."""
    from unified_agent import storage

    storage.init_db()
    storage.join_init_background_threads(timeout=5.0)
    conn = storage.connect()
    yield conn
    conn.close()


class TestInsertSyntheticBatch:
    def test_inserts_through_the_real_schema(self, bench_conn):
        import random

        rng = random.Random(1)
        ids = bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=25)
        assert len(ids) == 25
        count = bench_conn.execute(
            "SELECT COUNT(*) AS n FROM memory WHERE id LIKE 'bench-%'"
        ).fetchone()["n"]
        assert count == 25
        # FTS trigger must have fired for bulk-inserted rows too.
        fts_count = bench_conn.execute("SELECT COUNT(*) AS n FROM memory_fts").fetchone()["n"]
        assert fts_count == 25
        row = bench_conn.execute("SELECT tags FROM memory WHERE id = 'bench-00000000'").fetchone()
        assert json.loads(row["tags"])[0].startswith("topic:")

    def test_sample_queries_returns_multi_word_spans(self, bench_conn):
        import random

        rng = random.Random(2)
        bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=10)
        queries = bench.sample_queries_from_corpus(bench_conn, 5, rng)
        assert len(queries) == 5
        for q in queries:
            assert len(q.split()) >= 4


@pytest.mark.embeddings
class TestEmbedMissingNotes:
    def test_embeds_every_note_and_mirrors_into_memory_vec(self, bench_conn):
        import random

        from unified_agent import storage

        rng = random.Random(5)
        ids = bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=6)

        stats = bench.embed_missing_notes(bench_conn, batch_size=4)
        assert stats["embedded"] == 6
        assert stats["failures"] == 0
        assert stats["elapsed_s"] >= 0

        embedded_ids = {
            r["note_id"]
            for r in bench_conn.execute("SELECT note_id FROM memory_embeddings").fetchall()
        }
        assert embedded_ids == set(ids)

        # memory_vec mirror is best-effort (sqlite-vec may be unavailable on
        # this connection); assert parity only when the table exists at all.
        try:
            vec_ids = {
                r["id"]
                for r in bench_conn.execute(
                    "SELECT m.id AS id FROM memory_vec v JOIN memory m ON m.rowid = v.rowid"
                ).fetchall()
            }
        except storage.sqlite3.OperationalError:
            vec_ids = None
        if vec_ids is not None:
            assert vec_ids == set(ids)

    def test_second_call_is_a_noop_when_nothing_missing(self, bench_conn):
        import random

        rng = random.Random(6)
        bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=3)
        bench.embed_missing_notes(bench_conn, batch_size=10)
        stats = bench.embed_missing_notes(bench_conn, batch_size=10)
        assert stats["embedded"] == 0


@pytest.mark.embeddings
class TestMeasureRecallAndLatency:
    def test_engine_matches_brute_force_ground_truth_exactly(self, bench_conn):
        """The core claim the report leans on: at small scale, the engine
        memory_search actually calls scores identically to an independent
        brute-force cosine-similarity pass — proving the current pipeline
        does not already approximate before scoring all candidates."""
        import random

        rng = random.Random(9)
        bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=8)
        bench.embed_missing_notes(bench_conn, batch_size=8)

        queries = bench.sample_queries_from_corpus(bench_conn, 3, rng)
        recall = bench.measure_recall(bench_conn, queries, k=5)

        assert recall["n_queries"] == 3
        assert recall["engine_compared_rows"] > 0
        assert recall["engine_max_abs_similarity_diff"] < 1e-4

    def test_measure_query_latency_returns_percentiles(self, bench_conn):
        import random

        rng = random.Random(10)
        bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=5)
        bench.embed_missing_notes(bench_conn, batch_size=5)
        queries = bench.sample_queries_from_corpus(bench_conn, 3, rng)

        latency = bench.measure_query_latency(queries, limit=5)
        assert latency["n"] == 3
        assert latency["p50_ms"] >= 0
        assert latency["p99_ms"] >= latency["p50_ms"]


class TestDbFootprint:
    def test_reports_file_and_table_bytes(self, bench_conn, tmp_path):
        import random

        from unified_agent import config

        rng = random.Random(4)
        bench.insert_synthetic_batch(bench_conn, rng, start_index=0, count=5)
        bench_conn.close()  # release the WAL writer before checkpointing

        footprint = bench.db_footprint(config.db_path())
        assert footprint["file_bytes"] > 0
        assert footprint["memory_table_bytes"] > 0


# --- scripts/bench_memory_search.py reporting (task #2488) -------------------


class TestLatencyBenchStoreIdentity:
    """A store belongs to the one workload (notes, seed) it was built from."""

    def test_empty_home_is_built(self, tmp_path):
        assert latency_bench.check_store(tmp_path, notes=2, seed=99, saved=0) is False

    def test_notes_without_a_workload_record_are_refused(self, tmp_path):
        with pytest.raises(SystemExit, match="5 note"):
            latency_bench.check_store(tmp_path, notes=2, seed=99, saved=5)

    def test_another_workload_is_refused_by_name(self, tmp_path):
        latency_bench.write_workload(tmp_path, notes=5, seed=1)
        for notes, seed in ((2, 99), (5, 99), (2, 1)):
            with pytest.raises(SystemExit, match="5 notes, seed 1"):
                latency_bench.check_store(tmp_path, notes=notes, seed=seed, saved=5)

    def test_same_workload_resumes_then_is_reused(self, tmp_path):
        latency_bench.write_workload(tmp_path, notes=5, seed=1)
        assert latency_bench.check_store(tmp_path, notes=5, seed=1, saved=3) is False
        latency_bench.store_marker(tmp_path, 5, 1).write_text("{}")
        assert latency_bench.check_store(tmp_path, notes=5, seed=1, saved=5) is True
        with pytest.raises(SystemExit, match="holds 6"):
            latency_bench.check_store(tmp_path, notes=5, seed=1, saved=6)

    def test_store_from_before_the_workload_record_needs_one_matching_marker(self, tmp_path):
        latency_bench.store_marker(tmp_path, 5, 1).write_text("{}")
        assert latency_bench.check_store(tmp_path, notes=5, seed=1, saved=5) is True
        # A second marker over the same notes: one of them is a mislabel.
        latency_bench.store_marker(tmp_path, 5, 99).write_text("{}")
        with pytest.raises(SystemExit, match="no workload record"):
            latency_bench.check_store(tmp_path, notes=5, seed=99, saved=5)

    def test_build_store_does_not_relabel_another_runs_notes(self, tmp_path, monkeypatch):
        """Five notes left by an earlier run were reported as a two-note,
        seed-99 workload, and nothing was saved."""
        from unified_agent import embeddings, storage

        conn = storage.connect()
        try:
            for n in range(5):
                conn.execute(
                    "INSERT INTO memory(id,content,created_at,updated_at) VALUES (?,?,?,?)",
                    (f"old{n}", "left by an earlier run", storage.now(), storage.now()),
                )
            conn.commit()
        finally:
            conn.close()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(latency_bench, "_load_model", lambda _threads: 0.0)
        monkeypatch.setattr(
            embeddings, "generate_embeddings", lambda texts: [[1.0] + [0.0] * 383 for _ in texts]
        )
        with pytest.raises(SystemExit, match="5 note"):
            latency_bench.build_store(tmp_path, notes=2, seed=99, threads=0)
        assert not (home / "bench-store-2-99.json").exists()


@pytest.mark.parametrize(
    ("platform", "raw", "expected"),
    [("linux", 204_800, 200.0), ("darwin", 209_715_200, 200.0)],
)
def test_latency_bench_peak_rss_is_megabytes_on_each_platform(platform, raw, expected):
    """ru_maxrss is kilobytes on Linux and bytes on macOS (task #2488)."""
    assert latency_bench.maxrss_mb(raw, platform) == expected
