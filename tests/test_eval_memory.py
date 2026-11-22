"""Metric contract for the frozen memory evaluation runner."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_small_sample_p95_uses_nearest_rank():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_memory_1458.py"
    spec = importlib.util.spec_from_file_location("eval_memory_1458", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._percentile([1, 2, 3, 4, 5, 6], 0.95) == 6
    assert module._percentile([1, 2, 3, 4, 5, 6], 0.50) == 3


def test_fixture_exercises_supersession_and_freezes_index_dates(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_memory_1458.py"
    spec = importlib.util.spec_from_file_location("eval_memory_1458", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    subprocess.run(
        [sys.executable, str(path), "--seed-child", str(tmp_path)],
        env=module._env(tmp_path),
        check=True,
        capture_output=True,
    )
    ids = json.loads((tmp_path / "fixture-id-map.json").read_text())
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        indexes = conn.execute(
            "SELECT created_at, updated_at, content FROM memory WHERE tags LIKE '%meta:index%'"
        ).fetchall()
        frozen_at = module._fixture()["index_timestamp"]
        assert indexes and all(row[:2] == (frozen_at, frozen_at) for row in indexes)
        assert any("2026-03-01" in row[2] for row in indexes)

        # With supersession disabled, both old facts must match the same
        # queries. Otherwise the fixture could stay green after a regression.
        conn.execute(
            "UPDATE memory SET superseded_by=NULL WHERE id IN (?, ?)",
            (ids["m1458-temporal-old"], ids["m1458-broadcast-old"]),
        )
    result = subprocess.run(
        [sys.executable, str(path), "--query-child", str(tmp_path)],
        env=module._env(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    cases = {case["case"]: case for case in json.loads(result.stdout)["cases"]}
    assert cases["temporal-update"]["excluded_fixture_labels_present"] == ["m1458-temporal-old"]
    assert cases["contradictory-broadcast"]["excluded_fixture_labels_present"] == [
        "m1458-broadcast-old"
    ]


# --- ranking quality floor (task #1913) ------------------------------------------
# Ranking quality floor for memory_search on the frozen #1913 fixture.
#
# The floors sit below the measured post-#1913 numbers (lexical R@10 0.94 /
# MRR 0.83, hybrid 1.00 / 0.93) and well above the pre-#1913 baseline (lexical
# 0.38 / 0.37, hybrid 0.58 / 0.42), so a ranking regression fails here.


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_memory_1913.py"


def _runner():
    spec = importlib.util.spec_from_file_location("eval_memory_1913", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_covers_the_required_query_shapes():
    fixture = _runner().load_fixture()
    notes = {n["id"]: n for n in fixture["notes"]}
    assert 150 <= len(notes) <= 200
    assert 40 <= len(fixture["queries"]) <= 60
    categories = {q["category"] for q in fixture["queries"]}
    assert categories == {
        "paraphrase",
        "identifier",
        "multiword",
        "short",
        "popular",
        "superseded",
        "recency",
    }
    for case in fixture["queries"]:
        assert case["relevant"] and set(case["relevant"]) <= set(notes)
        for label in case["excluded"]:
            assert notes[label]["superseded_by"], label
    assert any(len(q["query"].split()) <= 2 for q in fixture["queries"])
    assert sum(1 for n in notes.values() if n["kind"] == "popular") >= 10


def _check(report, *, recall_at_10: float, mrr: float):
    overall = report["overall"]
    assert overall["recall_at_10"] >= recall_at_10, overall
    assert overall["mrr"] >= mrr, overall
    assert overall["excluded_leaks"] == 0
    # Exact identifiers must stay at rank 1.
    assert report["by_category"]["identifier"]["mrr"] == 1.0


def test_lexical_ranking_floor(monkeypatch):
    from unified_agent import embeddings

    # Record the real function so teardown restores it after run() stubs it out.
    monkeypatch.setattr(embeddings, "generate_embedding", embeddings.generate_embedding)
    report = _runner().run(semantic=False)
    _check(report, recall_at_10=0.9, mrr=0.75)


@pytest.mark.embeddings
def test_hybrid_ranking_floor():
    report = _runner().run(semantic=True)
    _check(report, recall_at_10=0.95, mrr=0.85)
