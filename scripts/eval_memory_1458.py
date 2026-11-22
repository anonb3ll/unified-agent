#!/usr/bin/env python3
"""Run the deterministic, sanitized native baseline for task #1458.

Uses a fresh temporary UA_HOME and production memory_save/memory_search entry
points. Semantic embeddings are disabled, so this is a repeatable lexical
baseline, not a full hybrid-memory scorecard. A child process reopens the DB
for the persisted restart-context case.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURE = ROOT / "tests/fixtures/memory_eval_1458/cases.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _env(home: Path) -> dict[str, str]:
    # Do not inherit provider credentials or live hub configuration in either
    # benchmark process. All writes are confined to this temporary home.
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "UA_HOME": str(home),
        "UA_WORKSPACE": str(home / "workspace"),
        "UA_VAULT_PATH": str(home / "vault"),
    }


def _configure(home: Path) -> None:
    env = _env(home)
    os.environ.clear()
    os.environ.update(env)


def _disable_embeddings() -> None:
    from unified_agent import embeddings

    embeddings.generate_embedding = lambda _text: (_ for _ in ()).throw(
        RuntimeError("offline fixture")
    )


def seed(home: Path) -> None:
    _configure(home)
    from unified_agent import config, storage
    from unified_agent.tools import memory as memory_tools
    from unified_agent.tools.memory import memory_save

    _disable_embeddings()
    from unified_agent import post_init

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
    fixture = _fixture()
    id_map = {}
    for note in fixture["notes"]:
        # Broadcast notes are human-only. The fixture still records them as
        # historical data; seed those rows as the configured human operator.
        broadcast = any(str(tag).strip().lower() == "broadcast" for tag in note["tags"])
        author = config.operator_label() if broadcast else "Desktop_Agent2"
        token = config.agent_identity_var.set(author) if broadcast else None
        try:
            saved = memory_save(
                title=note["title"],
                content=note["content"],
                tags=note["tags"],
                author=author,
            )
        finally:
            if token is not None:
                config.agent_identity_var.reset(token)
        id_map[note["id"]] = saved["id"]
    conn = storage.connect()
    try:
        for note in fixture["notes"]:
            conn.execute(
                "UPDATE memory SET source_host=?, created_at=?, updated_at=?, superseded_by=? WHERE id=?",
                (
                    note["source_host"],
                    note["created_at"],
                    note["created_at"],
                    id_map.get(note.get("superseded_by")),
                    id_map[note["id"]],
                ),
            )
        # memory_save creates tag/master indexes at wall-clock time. Rebuild
        # them after fixing note dates, then freeze their dates too so the
        # searchable fixture does not depend on seed time.
        index_ts = fixture["index_timestamp"]
        original_now = storage.now
        try:
            storage.now = lambda: index_ts
            memory_tools._refresh_indexes(
                conn, {tag for note in fixture["notes"] for tag in note["tags"]}
            )
        finally:
            storage.now = original_now
        conn.execute(
            "UPDATE memory SET created_at=? WHERE id=? OR id LIKE ?",
            (index_ts, memory_tools.MASTER_INDEX_ID, f"{memory_tools.TAG_INDEX_PREFIX}%"),
        )
        conn.commit()
    finally:
        conn.close()
    (home / "fixture-id-map.json").write_text(json.dumps(id_map), encoding="utf-8")


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * p) - 1)], 3) if ordered else 0.0


def query(home: Path) -> dict[str, Any]:
    _configure(home)
    from unified_agent.tools.memory import memory_search

    _disable_embeddings()
    from unified_agent import post_init

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
    id_map = json.loads((home / "fixture-id-map.json").read_text(encoding="utf-8"))
    outcomes = []
    for case in _fixture()["cases"]:
        started = time.perf_counter()
        result = memory_search(
            case["query"], tags=case.get("tags"), source_host=case.get("source_host"), limit=10
        )
        latency_ms = (time.perf_counter() - started) * 1000
        actual = [hit["id"] for hit in result["results"]]
        expected = {id_map[label] for label in case.get("expected_ids", [])}
        excluded = {id_map[label] for label in case.get("excluded_ids", [])}
        provenance_ok = (
            all(
                hit.get("source_host") == case["expected_source_host"]
                for hit in result["results"]
                if hit["id"] in expected
            )
            if case.get("expected_source_host")
            else None
        )
        outcomes.append(
            {
                "case": case["id"],
                "expected_fixture_labels": sorted(case.get("expected_ids", [])),
                "actual_ids": actual,
                "excluded_fixture_labels_present": sorted(
                    label for label in case.get("excluded_ids", []) if id_map[label] in actual
                ),
                "recall_at_10": len(expected.intersection(actual)) / len(expected)
                if expected
                else None,
                "provenance_ok": provenance_ok,
                "passed": expected.issubset(actual)
                and not excluded.intersection(actual)
                and provenance_ok is not False,
                "latency_ms": round(latency_ms, 3),
                "query_tokens": len(case["query"].split()),
                "result_tokens": sum(
                    len((hit.get("title") or "").split()) + len((hit.get("snippet") or "").split())
                    for hit in result["results"]
                ),
            }
        )
    return {
        "fixture_id": _fixture()["fixture_id"],
        "system": "unified-agent native memory_search (lexical-only fallback)",
        "embedding_mode": "disabled; offline fixture error forces FTS fallback",
        "external_api_calls": 0,
        "estimated_external_cost_usd": 0,
        "restart_check": "seed and query ran in separate exited Python processes",
        "cases": outcomes,
        "summary": {
            "case_count": len(outcomes),
            "passed": sum(bool(row["passed"]) for row in outcomes),
            "p50_latency_ms": _percentile([row["latency_ms"] for row in outcomes], 0.50),
            "p95_latency_ms": _percentile([row["latency_ms"] for row in outcomes], 0.95),
            "total_result_tokens": sum(row["result_tokens"] for row in outcomes),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Write JSON results here (default: stdout)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ua-memory-1458-") as raw_home:
        home = Path(raw_home)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--seed-child", str(home)],
            env=_env(home),
            check=True,
            capture_output=True,
            text=True,
        )
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--query-child", str(home)],
            env=_env(home),
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(child.stdout)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["summary"]["passed"] == report["summary"]["case_count"] else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--seed-child":
        seed(Path(sys.argv[2]))
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--query-child":
        print(json.dumps(query(Path(sys.argv[2])), sort_keys=True))
        raise SystemExit(0)
    raise SystemExit(main())
