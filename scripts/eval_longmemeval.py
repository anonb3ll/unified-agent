#!/usr/bin/env python3
"""LongMemEval retrieval benchmark for memory_search and vault_query (task #2145).

For each LongMemEval-S question the harness builds a fresh temporary
``UA_HOME`` (so it never opens the hub database), saves every haystack session
through production ``memory_save``, then runs the question through production
``memory_search`` and scores which sessions came back. A second backend writes
the same sessions as Markdown notes into a temporary vault and runs the
production ``vault_query`` ranking over it.

Dataset: ``longmemeval_s_cleaned.json`` from the Hugging Face dataset
``xiaowu0162/longmemeval-cleaned``, pinned by revision and sha256 (the same
file and hash GBrain's published LongMemEval number uses). The harness
downloads it into ``~/.cache/ua-longmemeval/<revision>/`` on first use and
refuses a file whose hash differs. The file is parsed as JSON data only.

Metrics, per question, computed over the session ids of the top-k returned
items (a turn-level note maps to its session):

- ``recall_all@k``: 1 when every labelled evidence session is in the top k
  (GBrain's strict headline metric);
- ``recall_any@k``: 1 when at least one evidence session is in the top k;
- ``recall_frac@k``: share of the evidence sessions in the top k.

Abstention questions (ids ending ``_abs``) are excluded, leaving 470 of 500.

    nice -n 10 uv run python scripts/eval_longmemeval.py --per-category 10 --seed 2145
    nice -n 10 uv run python scripts/eval_longmemeval.py --full --out report.json

Run ``--help`` for every option. The scorecard is docs/evals/longmemeval.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DATASET_REPO = "xiaowu0162/longmemeval-cleaned"
DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
DATASET_FILE = "longmemeval_s_cleaned.json"
DATASET_URL = (
    f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/{DATASET_REVISION}/{DATASET_FILE}"
)
DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DEFAULT_CACHE = Path("~/.cache/ua-longmemeval").expanduser() / DATASET_REVISION[:8]

KS = (5, 10)
SEARCH_LIMIT = 10
AUTHOR = "longmemeval-harness"
TAG = "project:longmemeval-eval"
CATEGORIES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "knowledge-update",
    "temporal-reasoning",
)
# UA_* variables a caller may set to study a ranking change; every other UA_*
# variable is cleared so the run is isolated from the host's configuration.
PASSTHROUGH_PREFIXES = ("UA_MEMORY_", "UA_EMBED_MODEL")


# --- pure helpers (unit-tested, no dataset, no model) -------------------------


def is_abstention(question_id: str) -> bool:
    return question_id.endswith("_abs")


def dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving unique items."""
    return list(dict.fromkeys(items))


def score_ranking(
    ranked_sessions: Sequence[str], evidence: Iterable[str], ks: Sequence[int] = KS
) -> dict[str, float]:
    """Score one ranked result list against the evidence session ids.

    ``ranked_sessions`` holds the session id of each returned item in rank
    order. Items, not distinct sessions, are cut at k, so two turns of one
    session use two of the k slots (the chunk-level protocol GBrain uses).
    """
    wanted = set(evidence)
    if not wanted:
        raise ValueError("a scored question needs at least one evidence session")
    out: dict[str, float] = {}
    for k in ks:
        found = wanted.intersection(ranked_sessions[:k])
        out[f"recall_all@{k}"] = 1.0 if found == wanted else 0.0
        out[f"recall_any@{k}"] = 1.0 if found else 0.0
        out[f"recall_frac@{k}"] = len(found) / len(wanted)
    first = next((i + 1 for i, s in enumerate(ranked_sessions) if s in wanted), None)
    out["first_hit_rank"] = float(first) if first else 0.0
    out["mrr"] = 1.0 / first if first else 0.0
    return out


def aggregate(rows: Sequence[dict[str, Any]], ks: Sequence[int] = KS) -> dict[str, Any]:
    """Mean of each metric plus hit counts, over a list of scored rows."""
    keys = [f"recall_{m}@{k}" for k in ks for m in ("all", "any", "frac")] + ["mrr"]
    out: dict[str, Any] = {"n": len(rows)}
    for key in keys:
        values = [r[key] for r in rows]
        out[key] = round(sum(values) / len(values), 4) if values else 0.0
    for k in ks:
        out[f"hits_all@{k}"] = int(sum(r[f"recall_all@{k}"] for r in rows))
    return out


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_cat = {c: aggregate([r for r in rows if r["category"] == c]) for c in CATEGORIES}
    return {
        "overall": aggregate(rows),
        "by_category": {c: v for c, v in by_cat.items() if v["n"]},
    }


def stratified_sample(
    questions: Sequence[dict[str, Any]], per_category: int, seed: int
) -> list[dict[str, Any]]:
    """Fixed-seed sample of ``per_category`` answerable questions per type.

    Questions are sorted by id inside each category before sampling, so the
    draw depends only on the dataset content and the seed.
    """
    rng = random.Random(seed)
    picked: list[dict[str, Any]] = []
    for category in CATEGORIES:
        pool = sorted(
            (
                q
                for q in questions
                if q["question_type"] == category and not is_abstention(q["question_id"])
            ),
            key=lambda q: q["question_id"],
        )
        picked.extend(rng.sample(pool, min(per_category, len(pool))))
    return picked


def parse_session_date(raw: str) -> str:
    """'2023/05/20 (Sat) 02:21' -> '2023-05-20T02:21:00+00:00' (dates are naive; UTC assumed)."""
    day, _weekday, clock = raw.split(" ", 2)
    stamp = datetime.strptime(f"{day} {clock}", "%Y/%m/%d %H:%M").replace(tzinfo=UTC)
    return stamp.isoformat()


def render_turn(turn: dict[str, Any]) -> str:
    return f"{turn['role']}: {turn['content'].strip()}"


def session_documents(
    question: dict[str, Any], granularity: str
) -> list[tuple[str, str, str, str]]:
    """(session_id, title, body, valid_from) per note to ingest.

    Titles and bodies carry no dataset session id: those mark evidence
    (``answer_*``) and would leak the label into lexical search.
    """
    docs = []
    triples = zip(
        question["haystack_session_ids"],
        question["haystack_dates"],
        question["haystack_sessions"],
        strict=True,
    )
    for sid, date, turns in triples:
        valid_from = parse_session_date(date)
        if granularity == "session":
            body = f"Conversation date: {date}\n\n" + "\n\n".join(render_turn(t) for t in turns)
            docs.append((sid, f"Conversation on {date}", body, valid_from))
        else:
            for n, turn in enumerate(turns, start=1):
                body = f"Conversation date: {date}\n\n{render_turn(turn)}"
                docs.append((sid, f"Conversation on {date}, turn {n}", body, valid_from))
    return docs


def evidence_diagnostics(
    question: dict[str, Any], ranked_sessions: Sequence[str]
) -> list[dict[str, Any]]:
    """Where each evidence session ranked, and where its answer sits in the note.

    ``answer_offset`` is the character offset of the first ``has_answer`` turn
    in the session-level note body. The embedding model reads only its first
    512 tokens (roughly 2,000 characters), so a large offset means the semantic
    leg never saw the evidence; only the lexical legs could find it.
    """
    wanted = set(question["answer_session_ids"])
    out = []
    triples = zip(
        question["haystack_session_ids"],
        question["haystack_dates"],
        question["haystack_sessions"],
        strict=True,
    )
    for sid, date, turns in triples:
        if sid not in wanted:
            continue
        offset = len(f"Conversation date: {date}\n\n")
        answer_offset = None
        for turn in turns:
            if turn.get("has_answer") and answer_offset is None:
                answer_offset = offset
            offset += len(render_turn(turn)) + 2
        rank = next((i + 1 for i, s in enumerate(ranked_sessions) if s == sid), None)
        out.append(
            {"session": sid, "rank": rank, "answer_offset": answer_offset, "chars": offset - 2}
        )
    return out


def vault_stem(index: int) -> str:
    """Opaque, deterministic note stem that no query term can match.

    vault_query adds score when a query term (a run of 2+ letters or digits)
    is a substring of the stem, so the stem must hold no two adjacent letters
    or digits: ``vault_stem(47) == "s_0_0_4_7"``. Fixed width keeps sorted
    file order equal to haystack order.
    """
    if not 0 <= index < 10_000:
        raise ValueError("vault_stem supports indexes 0..9999")
    return "s_" + "_".join(f"{index:04d}")


def select_ids(questions: Sequence[dict[str, Any]], raw: str) -> list[dict[str, Any]]:
    """Questions named in a comma list; refuse unknown or abstention ids."""
    wanted = {i.strip() for i in raw.split(",") if i.strip()}
    known = {q["question_id"]: q for q in questions}
    unknown = sorted(wanted - known.keys())
    abstention = sorted(i for i in wanted if i in known and is_abstention(i))
    problems = []
    if unknown:
        problems.append(f"unknown question ids: {', '.join(unknown)}")
    if abstention:
        problems.append(f"abstention ids have no evidence to score: {', '.join(abstention)}")
    if not wanted:
        problems.append("--ids names no question")
    if problems:
        raise SystemExit("; ".join(problems))
    return [q for q in questions if q["question_id"] in wanted]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- dataset ------------------------------------------------------------------


def fetch_dataset(data: Path | None, cache_dir: Path) -> Path:
    """Return a verified local copy of the pinned dataset, downloading it once."""
    path = data or cache_dir / DATASET_FILE
    if not path.is_file():
        if data is not None:
            raise SystemExit(f"dataset file not found: {data}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        print(f"downloading {DATASET_URL}", file=sys.stderr)
        with urllib.request.urlopen(DATASET_URL, timeout=60) as resp, partial.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        partial.replace(path)
    actual = sha256_file(path)
    if actual != DATASET_SHA256:
        raise SystemExit(f"{path}: sha256 {actual} does not match pinned {DATASET_SHA256}")
    return path


def load_questions(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


# --- isolated environment -----------------------------------------------------


def isolate_environment(work: Path) -> None:
    """Point every UA_* path at ``work`` before unified_agent is imported."""
    for name in [n for n in os.environ if n.startswith("UA_")]:
        if not name.startswith(PASSTHROUGH_PREFIXES):
            del os.environ[name]
    os.environ.update(
        {
            "UA_HOME": str(work / "home"),
            "UA_WORKSPACE": str(work / "workspace"),
            "UA_VAULT_PATH": str(work / "vault"),
            "UA_SETTINGS_FILE": "",
            "UA_STATE_SYNC": "0",
        }
    )
    (work / "workspace").mkdir(parents=True, exist_ok=True)
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))


def fresh_home(work: Path, label: str) -> Path:
    home = work / "homes" / label
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    os.environ["UA_HOME"] = str(home)
    return home


class EmbeddingCache:
    """Memoize generate_embedding by exact text.

    The model is deterministic, so a cached vector is the vector production
    would compute; only repeated inference over sessions shared between
    haystacks is skipped. Every call site in tools/memory.py imports the
    function from the module at call time, so patching the module attribute
    covers memory_save and memory_search alike.
    """

    def __init__(self, inner: Callable[[str], list[float]]):
        self.inner = inner
        self.store: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0

    def __call__(self, text: str) -> list[float]:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = self.store.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        vector = self.inner(text)
        self.store[key] = vector
        return vector


def prepare_embeddings(
    *, semantic: bool, threads: int | None, cache: bool
) -> EmbeddingCache | None:
    from unified_agent import config, embeddings

    if not semantic:
        # Chunk repair calls generate_embeddings, which this stub does not
        # cover, and that call loads the model. The flag makes a lexical run
        # write no chunk rows and never construct the model.
        os.environ["UA_MEMORY_CHUNK_EMBEDDINGS"] = "0"

        def _offline(_text: str):
            raise RuntimeError("lexical-only evaluation")

        embeddings.generate_embedding = _offline
        return None
    if threads:
        # Same model and constructor as get_embedding_model(); only the ONNX
        # thread count is pinned so the run stays polite on a shared host.
        from fastembed import TextEmbedding

        embeddings._model = TextEmbedding(model_name=config.embed_model(), threads=threads)
        embeddings._loaded_model_name = config.embed_model()
    embeddings.generate_embedding("probe")  # fail fast if the model is unavailable
    if not cache:
        return None
    wrapper = EmbeddingCache(embeddings.generate_embedding)
    embeddings.generate_embedding = wrapper
    return wrapper


# --- backends -----------------------------------------------------------------


def run_memory(question: dict[str, Any], work: Path, granularity: str) -> dict[str, Any]:
    """Ingest via memory_save into a fresh store and rank via memory_search."""
    from unified_agent import post_init, storage
    from unified_agent.tools.memory import memory_save, memory_search

    home = fresh_home(work, f"{question['question_id']}-{granularity}")
    post_init.init_db()
    storage.join_init_background_threads()
    session_of: dict[str, str] = {}
    t0 = time.perf_counter()
    for sid, title, body, valid_from in session_documents(question, granularity):
        saved = memory_save(
            content=body, title=title, tags=[TAG], author=AUTHOR, valid_from=valid_from
        )
        session_of[saved["id"]] = sid
    ingest_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    result = memory_search(question["question"], limit=SEARCH_LIMIT)
    search_s = time.perf_counter() - t1
    ranked = [session_of.get(hit["id"], "?") for hit in result["results"]]
    storage.join_init_background_threads()
    shutil.rmtree(home, ignore_errors=True)
    return {
        "ranked_sessions": ranked,
        "notes": len(session_of),
        "retrieval_mode": result.get("retrieval", {}).get("mode"),
        "ingest_s": round(ingest_s, 2),
        "search_s": round(search_s, 3),
    }


def run_vault(question: dict[str, Any], work: Path, granularity: str) -> dict[str, Any]:
    """Write sessions as Markdown notes into a fresh vault and rank via vault_query."""
    from unified_agent.tools.vault_graph import vault_query

    vault = work / "vaults" / f"{question['question_id']}-{granularity}"
    if vault.exists():
        shutil.rmtree(vault)
    (vault / "Wiki").mkdir(parents=True)
    os.environ["UA_VAULT_PATH"] = str(vault)
    session_of: dict[str, str] = {}
    for idx, (sid, title, body, _valid_from) in enumerate(session_documents(question, granularity)):
        stem = vault_stem(idx)
        (vault / "Wiki" / f"{stem}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        session_of[f"Wiki/{stem}.md"] = sid
    t1 = time.perf_counter()
    result = vault_query(question["question"], max_results=SEARCH_LIMIT)
    search_s = time.perf_counter() - t1
    ranked = [session_of.get(hit["path"], "?") for hit in result["results"]]
    shutil.rmtree(vault, ignore_errors=True)
    return {"ranked_sessions": ranked, "notes": len(session_of), "search_s": round(search_s, 3)}


BACKENDS: dict[str, Callable[[dict[str, Any], Path, str], dict[str, Any]]] = {
    "memory_search": run_memory,
    "vault_query": run_vault,
}


# --- driver -------------------------------------------------------------------


def ranking_settings() -> dict[str, Any]:
    from unified_agent import config
    from unified_agent import memory_ranking as ranking

    return {
        "embed_model": config.embed_model(),
        "rrf_k": ranking.rrf_k(),
        "fts_limit": ranking.fts_limit(),
        "fts_or_threshold": ranking.fts_or_threshold(),
        "semantic_min_similarity": ranking.semantic_min_similarity(),
        "semantic_top_k": ranking.semantic_top_k(),
        "leg_weights": ranking.leg_weights(),
        "ua_overrides": {
            n: os.environ[n] for n in sorted(os.environ) if n.startswith("UA_MEMORY_")
        },
    }


def evaluate(
    questions: Sequence[dict[str, Any]],
    *,
    work: Path,
    backends: Sequence[str],
    granularities: Sequence[str],
    progress: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    runs: dict[str, list[dict[str, Any]]] = {}
    for backend in backends:
        for granularity in granularities:
            key = f"{backend}/{granularity}"
            rows = []
            started = time.perf_counter()
            for i, q in enumerate(questions, start=1):
                out = BACKENDS[backend](q, work, granularity)
                row = {
                    "question_id": q["question_id"],
                    "category": q["question_type"],
                    "question": q["question"],
                    "evidence": list(q["answer_session_ids"]),
                    **out,
                    **score_ranking(out["ranked_sessions"], q["answer_session_ids"]),
                    "evidence_diag": evidence_diagnostics(q, out["ranked_sessions"]),
                }
                rows.append(row)
                if progress:
                    elapsed = time.perf_counter() - started
                    print(
                        f"[{key}] {i}/{len(questions)} {q['question_id']} "
                        f"all@5={row['recall_all@5']:.0f} any@5={row['recall_any@5']:.0f} "
                        f"elapsed={elapsed:.0f}s",
                        file=sys.stderr,
                        flush=True,
                    )
            runs[key] = rows
    return runs


def _table(title: str, summary: dict[str, Any]) -> str:
    lines = [
        title,
        f"{'slice':<26} {'n':>4} {'all@5':>6} {'any@5':>6} {'all@10':>6} {'any@10':>6} "
        f"{'frac@5':>6} {'MRR':>6}",
    ]
    for name, agg in [("overall", summary["overall"]), *summary["by_category"].items()]:
        lines.append(
            f"{name:<26} {agg['n']:>4} {agg['recall_all@5']:>6.3f} {agg['recall_any@5']:>6.3f} "
            f"{agg['recall_all@10']:>6.3f} {agg['recall_any@10']:>6.3f} "
            f"{agg['recall_frac@5']:>6.3f} {agg['mrr']:>6.3f}"
        )
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        parsed = 0
    if parsed < 1:
        raise argparse.ArgumentTypeError("a positive integer is required")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--full", action="store_true", help="all 470 answerable questions")
    sel.add_argument(
        "--per-category", type=_positive_int, default=10, help="stratified sample size"
    )
    sel.add_argument("--ids", help="comma-separated question ids to run")
    parser.add_argument("--seed", type=int, default=2145, help="sample seed (default 2145)")
    parser.add_argument(
        "--backends",
        default="memory_search,vault_query",
        help=f"comma list from {sorted(BACKENDS)}",
    )
    parser.add_argument("--granularity", default="session", help="session, turn, or session,turn")
    parser.add_argument("--lexical", action="store_true", help="disable the semantic leg")
    parser.add_argument("--threads", type=int, default=2, help="ONNX threads (0 = library default)")
    parser.add_argument("--no-embed-cache", action="store_true", help="re-embed repeated text")
    parser.add_argument("--data", type=Path, help="use this local copy of the dataset file")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--work-dir", type=Path, help="scratch root (default: under --cache-dir)")
    parser.add_argument("--out", type=Path, help="write the full JSON report here")
    parser.add_argument("--quiet", action="store_true", help="no per-question progress")
    args = parser.parse_args(argv)

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    granularities = [g.strip() for g in args.granularity.split(",") if g.strip()]
    for b in backends:
        if b not in BACKENDS:
            parser.error(f"unknown backend {b!r}")
    for g in granularities:
        if g not in ("session", "turn"):
            parser.error(f"unknown granularity {g!r}")

    path = fetch_dataset(args.data, args.cache_dir)
    questions = load_questions(path)
    answerable = [q for q in questions if not is_abstention(q["question_id"])]
    if args.ids is not None:
        chosen = select_ids(questions, args.ids)
        selection = {"mode": "ids", "ids": sorted(q["question_id"] for q in chosen)}
    elif args.full:
        chosen = answerable
        selection = {"mode": "full"}
    else:
        chosen = stratified_sample(questions, args.per_category, args.seed)
        selection = {"mode": "stratified", "per_category": args.per_category, "seed": args.seed}

    tmp = None
    if args.work_dir:
        work = args.work_dir
        work.mkdir(parents=True, exist_ok=True)
    else:
        # Beside the dataset, not in the system temp dir: notes are derived data.
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(prefix="work-", dir=args.cache_dir)
        work = Path(tmp.name)
    try:
        isolate_environment(work)
        cache = None
        if "memory_search" in backends:
            cache = prepare_embeddings(
                semantic=not args.lexical,
                threads=args.threads or None,
                cache=not args.no_embed_cache,
            )
        started = time.perf_counter()
        runs = evaluate(
            chosen,
            work=work,
            backends=backends,
            granularities=granularities,
            progress=not args.quiet,
        )
        wall = time.perf_counter() - started
        report = {
            "task": 2145,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": {
                "url": DATASET_URL,
                "sha256": DATASET_SHA256,
                "questions": len(questions),
                "answerable": len(answerable),
            },
            "selection": {**selection, "n": len(chosen)},
            "settings": {
                "search_limit": SEARCH_LIMIT,
                "ks": list(KS),
                "semantic": not args.lexical,
                "onnx_threads": args.threads or None,
                "embed_cache": cache is not None,
                **(ranking_settings() if "memory_search" in backends else {}),
            },
            "wall_s": round(wall, 1),
            "embed_cache": (
                {"hits": cache.hits, "misses": cache.misses} if cache is not None else None
            ),
            "runs": {key: {**summarize(rows), "questions": rows} for key, rows in runs.items()},
        }
    finally:
        if tmp is not None:
            tmp.cleanup()

    print(f"{len(chosen)} questions, wall {report['wall_s']}s")
    for key, run in report["runs"].items():
        print(_table(key, run))
        misses = [r for r in run["questions"] if not r["recall_all@5"]]
        for r in misses:
            ranks = " ".join(
                f"{d['rank'] or '-'}@{'n/l' if d['answer_offset'] is None else d['answer_offset']}"
                for d in r["evidence_diag"]
            )
            print(
                f"  miss@5 {r['question_id']} {r['category']} evidence rank@answer_offset {ranks}"
            )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
