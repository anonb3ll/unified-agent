"""Scoring helpers of the LongMemEval harness (task #2145).

The scoring helpers are pure. The lexical-prepare test uses an isolated
home and never downloads the dataset or the embedding model.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_longmemeval.py"


@pytest.fixture(scope="module")
def lme():
    spec = importlib.util.spec_from_file_location("eval_longmemeval", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recall_all_needs_every_evidence_session(lme):
    ranked = ["x", "a", "y", "z", "w", "b"]
    s = lme.score_ranking(ranked, ["a", "b"])
    assert s["recall_any@5"] == 1.0
    assert s["recall_all@5"] == 0.0
    assert s["recall_frac@5"] == 0.5
    assert s["recall_all@10"] == 1.0
    assert s["first_hit_rank"] == 2.0
    assert s["mrr"] == 0.5


def test_items_not_distinct_sessions_fill_the_k_slots(lme):
    # Turn-level notes: five turns of one session use all five slots.
    ranked = ["a"] * 5 + ["b"]
    s = lme.score_ranking(ranked, ["a", "b"])
    assert s["recall_all@5"] == 0.0
    assert s["recall_all@10"] == 1.0


def test_no_hit_scores_zero(lme):
    s = lme.score_ranking(["x", "y"], ["a"])
    assert s["recall_any@10"] == 0.0
    assert s["first_hit_rank"] == 0.0
    assert s["mrr"] == 0.0


def test_empty_evidence_is_refused(lme):
    with pytest.raises(ValueError):
        lme.score_ranking(["a"], [])


def test_aggregate_and_summary(lme):
    rows = [
        {"category": "multi-session", **lme.score_ranking(["a", "b"], ["a", "b"])},
        {"category": "multi-session", **lme.score_ranking(["a"], ["a", "b"])},
        {"category": "knowledge-update", **lme.score_ranking(["z"], ["a"])},
    ]
    summary = lme.summarize(rows)
    assert summary["overall"]["n"] == 3
    assert summary["overall"]["hits_all@5"] == 1
    assert summary["overall"]["recall_any@5"] == pytest.approx(0.6667)
    assert set(summary["by_category"]) == {"multi-session", "knowledge-update"}
    assert summary["by_category"]["multi-session"]["recall_all@5"] == 0.5


def _question(qid, qtype):
    return {"question_id": qid, "question_type": qtype}


def test_stratified_sample_is_seeded_and_skips_abstention(lme):
    pool = [_question(f"q{i}", c) for c in lme.CATEGORIES for i in range(5)]
    pool += [_question(f"q{i}_abs", "multi-session") for i in range(5)]
    first = lme.stratified_sample(pool, 2, seed=7)
    again = lme.stratified_sample(list(reversed(pool)), 2, seed=7)
    assert [q["question_id"] for q in first] == [q["question_id"] for q in again]
    assert len(first) == 2 * len(lme.CATEGORIES)
    assert not any(lme.is_abstention(q["question_id"]) for q in first)


def test_documents_hide_session_ids_and_parse_dates(lme):
    q = {
        "question_id": "q1",
        "answer_session_ids": ["answer_1"],
        "haystack_session_ids": ["answer_1", "noise_2"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 10:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yes"}],
            [{"role": "user", "content": "other", "has_answer": False}],
        ],
    }
    sessions = lme.session_documents(q, "session")
    assert [d[0] for d in sessions] == ["answer_1", "noise_2"]
    assert sessions[0][3] == "2023-05-20T02:21:00+00:00"
    assert all("answer_1" not in d[1] + d[2] for d in sessions)
    turns = lme.session_documents(q, "turn")
    assert [d[0] for d in turns] == ["answer_1", "answer_1", "noise_2"]

    q["haystack_sessions"][0][1]["has_answer"] = True
    diag = lme.evidence_diagnostics(q, ["noise_2", "answer_1"])
    assert diag == [
        {
            "session": "answer_1",
            "rank": 2,
            "answer_offset": len("Conversation date: 2023/05/20 (Sat) 02:21\n\nuser: hi\n\n"),
            "chars": len(sessions[0][2]),
        }
    ]


def test_vault_stems_are_deterministic_and_match_no_query_term(lme):
    import re

    stems = [lme.vault_stem(i) for i in range(1000)]
    assert stems[47] == "s_0_0_4_7"
    assert len(set(stems)) == len(stems)
    assert stems == sorted(stems)  # sorted file walk keeps haystack order
    # vault_query terms are runs of 2+ [a-z0-9]; a stem must contain none.
    assert not any(re.search(r"[a-z0-9]{2}", s) for s in stems)
    with pytest.raises(ValueError):
        lme.vault_stem(10_000)


def test_select_ids_refuses_unknown_and_abstention_ids(lme):
    pool = [_question("q1", "multi-session"), _question("q2_abs", "multi-session")]
    assert [q["question_id"] for q in lme.select_ids(pool, " q1 ,")] == ["q1"]
    with pytest.raises(SystemExit, match="unknown question ids: nope"):
        lme.select_ids(pool, "q1,nope")
    with pytest.raises(SystemExit, match="abstention ids"):
        lme.select_ids(pool, "q2_abs")
    with pytest.raises(SystemExit, match="names no question"):
        lme.select_ids(pool, " , ")


def test_per_category_rejects_zero_and_negative(lme, capsys):
    for raw in ("0", "-1"):
        with pytest.raises(SystemExit) as exc:
            lme.main(["--per-category", raw])
        assert exc.value.code == 2
        assert "positive integer" in capsys.readouterr().err


def test_lexical_prepare_does_not_load_the_model_or_write_chunks(lme, isolated_env, monkeypatch):
    from unified_agent import embeddings, storage
    from unified_agent.tools.memory import memory_save

    loads = {"n": 0}

    def _refuse(*_args, **_kwargs):
        loads["n"] += 1
        raise AssertionError("embedding model loaded")

    monkeypatch.setattr(embeddings, "get_embedding_model", _refuse)
    # prepare_embeddings assigns module attributes directly. Record the
    # originals first so the fixture restores them after that assignment.
    monkeypatch.setattr(embeddings, "generate_embedding", embeddings.generate_embedding)
    monkeypatch.setattr(embeddings, "generate_embeddings", embeddings.generate_embeddings)
    monkeypatch.setenv(
        "UA_MEMORY_CHUNK_EMBEDDINGS",
        os.environ.get("UA_MEMORY_CHUNK_EMBEDDINGS", "1"),
    )

    assert lme.prepare_embeddings(semantic=False, threads=None, cache=False) is None
    assert os.environ["UA_MEMORY_CHUNK_EMBEDDINGS"] == "0"

    saved = memory_save("n" * 2_101, title="Lexical", author="note-owner")
    assert loads["n"] == 0
    conn = storage.connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE note_id=?", (saved["id"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()
