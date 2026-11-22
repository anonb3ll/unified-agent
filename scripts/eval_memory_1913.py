#!/usr/bin/env python3
"""Ranking evaluation for memory_search (task #1913).

Seeds the frozen synthetic corpus in ``tests/fixtures/memory_eval_1913`` into a
fresh temporary ``UA_HOME`` through production ``memory_save``, then sets each
note's age, access count, recall votes, feedback and supersession directly in
that throwaway database. Every query runs through production ``memory_search``
with ``limit=10``. Search side effects (the access-count bump, recall votes)
are reset to the fixture values before each query, so the queries are
independent and the run is deterministic.

Metrics, per query and averaged overall and per category:

- ``recall@5`` / ``recall@10``: share of the labelled relevant notes returned
  in the top 5 / top 10;
- ``mrr``: reciprocal rank of the first relevant note (0 when none is in the
  top 10);
- ``excluded_leaks``: labelled superseded notes that appeared at all.

Two modes. The default ``hybrid`` mode needs the fastembed model already in
the local cache (it never downloads one: set ``HF_HUB_OFFLINE=1`` and point
``TMPDIR`` at the directory holding ``fastembed_cache``). ``--lexical`` disables
embeddings, so it runs anywhere and exercises the FTS and entity legs only.

    uv run python scripts/eval_memory_1913.py [--lexical] [--out PATH] [--per-query]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/memory_eval_1913/corpus.json"
AUTHOR = "Desktop_Agent2"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def disable_embeddings() -> None:
    from unified_agent import embeddings

    def _offline(_text: str):
        raise RuntimeError("lexical-only evaluation")

    embeddings.generate_embedding = _offline


def _apply_stats(conn, fixture: dict[str, Any], id_map: dict[str, str]) -> None:
    """Put access counts and recall votes back to the fixture values."""
    conn.execute("DELETE FROM memory_recall_votes")
    for note in fixture["notes"]:
        nid = id_map[note["id"]]
        conn.execute(
            "UPDATE memory SET access_count=?, last_accessed=NULL WHERE id=?",
            (note["access_count"], nid),
        )
        if note["recalled"] or note["upvoted"]:
            conn.execute(
                "INSERT INTO memory_recall_votes (note_id, recalled_count, upvoted_count, "
                "last_recalled_at) VALUES (?, ?, ?, NULL)",
                (nid, note["recalled"], note["upvoted"]),
            )


def seed(fixture: dict[str, Any]) -> dict[str, str]:
    """Write the corpus into the current UA_HOME; return fixture label -> note id."""
    from unified_agent import post_init, storage
    from unified_agent.tools.memory import memory_save

    post_init.init_db()
    id_map: dict[str, str] = {}
    for note in fixture["notes"]:
        saved = memory_save(
            title=note["title"], content=note["content"], tags=note["tags"], author=AUTHOR
        )
        id_map[note["id"]] = saved["id"]
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        for note in fixture["notes"]:
            stamp = (now - timedelta(days=note["age_days"])).isoformat()
            conn.execute(
                "UPDATE memory SET created_at=?, updated_at=?, salience=?, superseded_by=?, "
                "feedback_helpful=?, feedback_not_helpful=? WHERE id=?",
                (
                    stamp,
                    stamp,
                    note["salience"],
                    id_map.get(note["superseded_by"]) if note["superseded_by"] else None,
                    note["feedback_helpful"],
                    note["feedback_not_helpful"],
                    id_map[note["id"]],
                ),
            )
        _apply_stats(conn, fixture, id_map)
        conn.commit()
    finally:
        conn.close()
    return id_map


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "queries": len(rows),
        "recall_at_5": _mean([r["recall_at_5"] for r in rows]),
        "recall_at_10": _mean([r["recall_at_10"] for r in rows]),
        "mrr": _mean([r["reciprocal_rank"] for r in rows]),
        "excluded_leaks": sum(len(r["excluded_present"]) for r in rows),
    }


def evaluate(fixture: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """Run every fixture query through memory_search and score it."""
    from unified_agent import storage
    from unified_agent.tools.memory import memory_search

    label_of = {nid: label for label, nid in id_map.items()}
    rows = []
    for case in fixture["queries"]:
        conn = storage.connect()
        try:
            _apply_stats(conn, fixture, id_map)
            conn.commit()
        finally:
            conn.close()
        result = memory_search(case["query"], limit=10)
        ranked = [label_of.get(hit["id"], hit["id"]) for hit in result["results"]]
        relevant = set(case["relevant"])
        first = next((i + 1 for i, label in enumerate(ranked) if label in relevant), None)
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "query": case["query"],
                "relevant": case["relevant"],
                "ranked": ranked,
                "first_relevant_rank": first,
                "recall_at_5": len(relevant.intersection(ranked[:5])) / len(relevant),
                "recall_at_10": len(relevant.intersection(ranked[:10])) / len(relevant),
                "reciprocal_rank": 1.0 / first if first else 0.0,
                "excluded_present": sorted(set(case["excluded"]).intersection(ranked)),
            }
        )
    categories = sorted({r["category"] for r in rows})
    return {
        "overall": _aggregate(rows),
        "by_category": {c: _aggregate([r for r in rows if r["category"] == c]) for c in categories},
        "queries": rows,
    }


def run(*, semantic: bool) -> dict[str, Any]:
    """Seed and evaluate in the already-configured UA_HOME (used by the tests)."""
    if semantic:
        from unified_agent import embeddings

        embeddings.generate_embedding("probe")  # fail fast if the model is unavailable
    else:
        disable_embeddings()
    fixture = load_fixture()
    id_map = seed(fixture)
    report = evaluate(fixture, id_map)
    report["fixture_id"] = fixture["fixture_id"]
    report["mode"] = "hybrid" if semantic else "lexical"
    report["notes"] = len(fixture["notes"])
    return report


def _table(report: dict[str, Any]) -> str:
    lines = [
        f"{report['fixture_id']} mode={report['mode']} notes={report['notes']}",
        f"{'slice':<12} {'n':>3} {'R@5':>6} {'R@10':>6} {'MRR':>6} {'leaks':>5}",
    ]
    for name, agg in [("overall", report["overall"]), *report["by_category"].items()]:
        lines.append(
            f"{name:<12} {agg['queries']:>3} {agg['recall_at_5']:>6.3f} "
            f"{agg['recall_at_10']:>6.3f} {agg['mrr']:>6.3f} {agg['excluded_leaks']:>5}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lexical", action="store_true", help="disable embeddings")
    parser.add_argument("--out", type=Path, help="write the full JSON report here")
    parser.add_argument("--per-query", action="store_true", help="print per-query ranks")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ua-memory-1913-") as raw:
        home = Path(raw)
        os.environ.update(
            {
                "UA_HOME": str(home),
                "UA_WORKSPACE": str(home / "workspace"),
                "UA_VAULT_PATH": str(home / "vault"),
                "UA_SETTINGS_FILE": "",
                "UA_STATE_SYNC": "0",
            }
        )
        sys.path.insert(0, str(ROOT / "src"))
        report = run(semantic=not args.lexical)
    print(_table(report))
    if args.per_query:
        for row in report["queries"]:
            print(
                f"{row['id']} {row['category']:<11} rank={row['first_relevant_rank']} "
                f"{row['query']!r} top3={row['ranked'][:3]}"
            )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
