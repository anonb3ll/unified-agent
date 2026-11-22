#!/usr/bin/env python3
"""Per-leg latency benchmark for ``memory_search`` (task #2444).

Builds (or reuses) a synthetic store of hub size in a temp ``UA_HOME`` under
``~/.cache``, then runs one cold query and N warm queries through the real
``memory_search`` and reports wall time per retrieval leg, p50/p95 and peak
RSS. It never opens the hub database: ``UA_HOME`` and every other ``UA_*``
path point at the work directory before ``unified_agent`` is imported.

Leg timing wraps the functions each leg calls from this script only; the
product code carries no timers. ``--profile`` adds a cProfile of the warm
queries. ``--vault`` also times ``vault_search`` and ``vault_links`` over a
synthetic vault.

    OMP_NUM_THREADS=2 nice -n 10 python scripts/bench_memory_search.py \
        --notes 3000 --queries 40 --work ~/.cache/ua-bench-2444

The store is deterministic for a given ``--seed`` and ``--notes``: notes are
assembled from topic sentence templates, so the semantic and lexical legs see
realistic overlap. About 15% of notes are 5,000 to 20,000 characters, so
chunked embeddings (on by default since task #2182) engage.

A work directory holds one workload, the ``--notes`` and ``--seed`` it was
built from. A run that asks for another one is refused instead of reporting
the notes it found under the new label (task #2488); give it its own
``--work``.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import random
import resource
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOPICS = {
    "deploy": [
        "The deploy steward rolled the hub to {sha} after the batch CI went green.",
        "Hub restart on the Mac hub took {n} seconds and the launchd unit came back healthy.",
        "A failed deploy left the service on the previous release; rollback used the runbook.",
        "Deploy windows avoid the nightly full-suite run so the hub is not restarted mid-test.",
    ],
    "embeddings": [
        "fastembed loads bge-small-en-v1.5 lazily on the first semantic query.",
        "Chunked embeddings split long notes into windows of about 400 tokens with overlap.",
        "The sqlite-vec mirror memory_vec holds 384 float vectors for the KNN cut.",
        "Embedding a 512-token session costs about {n} milliseconds on two ONNX threads.",
    ],
    "vault": [
        "Obsidian Sync carries the vault between the desktop and the Mac hub.",
        "Inbox captures are ingested into Wiki pages by the steward after review.",
        "vault_links walks wikilinks and backlinks across about two thousand notes.",
        "Daily journal entries are appended by agents with their label and timestamp.",
    ],
    "tasks": [
        "Task #{task} was claimed by {agent} and moved to waiting_review with a PR.",
        "The WIP cap was raised to twenty so builders are not refused claims.",
        "A task under a cancelled parent cannot be claimed until it is reparented.",
        "Queue grooming closed {n} stale tasks that had no evidence reference.",
    ],
    "ci": [
        "CI runs four pytest shards plus desktop-core and the js job on the merge queue.",
        "Depot dispatch replaced GitHub Actions while billing blocked the hosted runners.",
        "The affected-tests script maps a diff to its import-graph dependents.",
        "A held PR with green CI needs a regression test the same day.",
    ],
    "security": [
        "The PreToolUse hook blocks commands that could print secrets such as env.",
        "Bearer tokens live in service.env and are never echoed into files.",
        "security_audit flags eval and exec in Python through check_py_ast.",
        "The workspace sandbox refuses writes outside UA_WORKSPACE and the task worktree.",
    ],
    "memory": [
        "memory_search fuses lexical, semantic, entity, recency and graph legs with RRF.",
        "Salience and feedback form a bounded prior between 0.8 and 1.2.",
        "Superseded notes are hidden from recall but stay readable by id.",
        "The FTS leg runs AND first and falls back to OR when AND finds few rows.",
    ],
    "railway": [
        "The Railway staging hub runs the same image as the Mac hub for smoke probes.",
        "Infrastructure as code for Railway keeps service variables out of the repo.",
        "A probe script checks the staging hub health endpoint every {n} minutes.",
        "Cloud agents reach staging over HTTPS with a per-agent token.",
    ],
    "sdk": [
        "The TypeScript SDK lives in packages/protocol and packages/client.",
        "The control center web app is built from packages/web with Vite.",
        "SDK clients call the HTTP JSON adapter rather than MCP.",
        "Protocol types are generated from the tool argument models.",
    ],
    "graph": [
        "Personalized PageRank over wikilinks seeds from lexical hits in vault_query.",
        "The memory graph links notes by shared entities such as task ids and services.",
        "One graph hop from the top hits adds notes the content legs missed.",
        "Entity document frequency down-weights hubs like project names.",
    ],
}
AGENTS = ["Desktop_Agent1", "Desktop_Agent2", "Desktop_Agent5", "Laptop_Agent1", "cloud_agent1"]
FILLER = [
    "Follow-up is tracked on the hub.",
    "The owner approved the change in review.",
    "The note was written after a long debugging session.",
    "Numbers came from the desktop with four shared cores.",
    "See the linked PR for the diff and the verification log.",
    "This replaces an earlier note that had the wrong figure.",
]
QUERIES = [
    "why is the deploy slow",
    "hub restart launchd",
    "embedding model load time",
    "chunked embeddings window size",
    "sqlite-vec KNN mirror",
    "obsidian sync vault",
    "inbox ingest wiki",
    "wip cap claims",
    "task claimed waiting_review",
    "ci shards merge queue",
    "depot dispatch billing",
    "affected tests import graph",
    "secrets hook env",
    "bearer token service.env",
    "sandbox workspace writes",
    "memory_search fusion legs",
    "salience feedback prior",
    "superseded notes recall",
    "fts and or fallback",
    "railway staging hub",
    "typescript sdk packages",
    "control center web app",
    "pagerank wikilinks",
    "graph hop entities",
    "task #1955 retrieval diagnostics",
    "Desktop_Agent5 review",
    "rollback runbook",
    "nightly full suite",
    "held PR regression test",
    "security audit eval exec",
    "what did we decide about the rust port",
    "how long does embedding a session take",
    "vault links backlinks",
    "journal entries agents",
    "probe script health endpoint",
    "protocol types generated",
    "entity document frequency",
    "queue grooming stale tasks",
    "cancelled parent reparent",
    "ONNX threads",
    "memory graph shared entities",
    "Cloud agents HTTPS token",
    "restart hub after tool change",
    "project:unified-agent decision",
    "consolidation duplicate notes",
    "evidence ledger tamper",
    "approval queue baseline",
    "per agent workspace",
    "Desktop_Agent2 adversarial review",
    "staging smoke probe",
    "task #2182 chunking recall",
    "bge small model truncation",
    "deploy steward batch",
    "pytest temp hygiene tmpfs",
    "Hyprland desktop keybindings",
    "longmemeval recall at five",
    "hub RSS memory",
    "vault_search latency",
    "memory_save slow",
    "kernel split rewrite",
]


def _note(rng: random.Random, i: int) -> tuple[str, str, list[str], int]:
    topic = rng.choice(list(TOPICS))
    roll = rng.random()
    if roll < 0.15:
        target = rng.randint(5_000, 20_000)
    elif roll < 0.25:
        target = rng.randint(2_000, 5_000)
    else:
        target = rng.randint(120, 1_900)
    # (template, rendered) pairs; the template keys the approximate vector.
    parts: list[tuple[str, str]] = []
    size = 0
    while size < target:
        if rng.random() < 0.7:
            tmpl = rng.choice(TOPICS[topic])
        elif rng.random() < 0.5:
            tmpl = rng.choice(TOPICS[rng.choice(list(TOPICS))])
        else:
            tmpl = rng.choice(FILLER)
        s = tmpl.format(
            sha=f"{rng.getrandbits(32):08x}",
            n=rng.randint(2, 900),
            task=rng.randint(100, 2500),
            agent=rng.choice(AGENTS),
        )
        if rng.random() < 0.15:
            s += "\n\n"
        parts.append((tmpl, s))
        size += len(s) + 1
    head = rng.choice(TOPICS[topic])
    title = f"{topic.title()} note {i}: " + _render(head)[:60]
    tags = [f"project:{rng.choice(['unified-agent', 'agent', 'gbrain'])}", topic]
    tags.append(rng.choice(["fact", "decision", "pref"]))
    if rng.random() < 0.3:
        tags.append(rng.choice(["deploy", "review", "perf", "infra"]))
    return title, head, parts, tags


def _render(tmpl: str) -> str:
    return tmpl.format(sha="0", n=1, task=1, agent=AGENTS[0])


def _approx_vectors(title_tmpl, parts, chunks, src, tvec, rng):
    """Approximate vectors: the normalized mean of the embedded sentence
    templates a text covers, plus a little noise so no two notes tie.

    Full-model embedding of a 3,000-note store with chunks is about 10,000
    texts, hours on a loaded 4-core desktop. Queries still run the real model.
    """
    import numpy as np

    spans = []
    pos = len(src) - len(" ".join(s for _t, s in parts))
    for tmpl, s in parts:
        spans.append((pos, pos + len(s), tmpl))
        pos += len(s) + 1

    def vec(keys):
        m = np.mean([tvec[k] for k in keys], axis=0)
        m = m + rng.normal(0.0, 0.01, size=m.shape)
        return (m / np.linalg.norm(m)).astype("float32").tolist()

    window = [title_tmpl] + [t for a, b, t in spans if a < 2048]
    single = vec(window)
    out = []
    for chunk in chunks:
        start = src.find(chunk)
        end = start + len(chunk)
        keys = [t for a, b, t in spans if a < end and b > start] or [title_tmpl]
        out.append(vec(keys))
    return single, out


def isolate(work: Path) -> None:
    for name in [n for n in os.environ if n.startswith("UA_")]:
        if name not in ("UA_MEMORY_CHUNK_EMBEDDINGS",):
            del os.environ[name]
    os.environ.update(
        {
            "UA_HOME": str(work / "home"),
            "UA_WORKSPACE": str(work / "workspace"),
            "UA_VAULT_PATH": str(work / "vault"),
            "UA_SETTINGS_FILE": "",
            "UA_STATE_SYNC": "0",
            "UA_AGENT_LABEL": "bench",
        }
    )
    (work / "workspace").mkdir(parents=True, exist_ok=True)
    (work / "home").mkdir(parents=True, exist_ok=True)
    (work / "vault").mkdir(parents=True, exist_ok=True)
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))


#: Records which workload a store was built from, before its first note is saved.
WORKLOAD_FILE = "bench-workload.json"


def store_marker(home: Path, notes: int, seed: int) -> Path:
    """Written with the row counts once the whole workload is saved."""
    return home / f"bench-store-{notes}-{seed}.json"


def write_workload(home: Path, notes: int, seed: int) -> None:
    (home / WORKLOAD_FILE).write_text(json.dumps({"notes": notes, "seed": seed}))


def check_store(home: Path, notes: int, seed: int, saved: int) -> bool:
    """True when the store under ``home`` is this finished workload, False to build it.

    ``saved`` is how many notes the store holds now; a build cut short resumes
    after them. Exits with the reason when the store is not this workload,
    because reusing it would report another run's notes under this label.
    """
    try:
        found = json.loads((home / WORKLOAD_FILE).read_text())
    except (OSError, ValueError):
        found = None
    done = store_marker(home, notes, seed).exists()
    if found is None:
        if not saved:
            return False
        # A store from before the workload record: its marker names it, unless
        # a second marker shows that one of them was written over the same notes.
        if saved == notes and list(home.glob("bench-store-*.json")) == [
            store_marker(home, notes, seed)
        ]:
            return True
        raise SystemExit(
            f"{home} holds {saved} note(s) and no workload record, so they cannot be "
            f"shown to be the {notes} notes of seed {seed}. Pass an empty --work."
        )
    if found != {"notes": notes, "seed": seed}:
        raise SystemExit(
            f"{home} was built for {found.get('notes')} notes, seed {found.get('seed')}, "
            f"not {notes} notes, seed {seed}. Pass another --work for this workload."
        )
    if saved > notes or (done and saved != notes):
        raise SystemExit(
            f"{home} holds {saved} note(s), which is not the {notes} of its workload: "
            "something else wrote to it. Pass an empty --work."
        )
    return done


def _saved_ids(storage) -> list[str]:
    conn = storage.connect()
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory WHERE id NOT LIKE 'masterindex%' "
                "AND id NOT LIKE 'tagindex_%' ORDER BY rowid"
            )
        ]
    finally:
        conn.close()


def build_store(work: Path, notes: int, seed: int, threads: int) -> None:
    from unified_agent import embeddings, memory_chunks, storage
    from unified_agent.tools import memory as mem

    home = work / "home"
    marker = store_marker(home, notes, seed)
    storage.init_db()
    # Resumable: a build cut short keeps its notes; continue after them. Links
    # and authors draw from a per-note rng so a resumed store is the same.
    ids = _saved_ids(storage)
    reuse = check_store(home, notes, seed, len(ids))
    write_workload(home, notes, seed)
    if reuse:
        print(f"reusing store: {marker}")
        return
    import numpy as np

    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    specs = [_note(rng, i) for i in range(notes)]
    templates = sorted({t for ts in TOPICS.values() for t in ts} | set(FILLER))
    _load_model(threads)
    t0 = time.perf_counter()
    tvec = {
        t: np.asarray(v, dtype="float64")
        for t, v in zip(
            templates, embeddings.generate_embeddings([_render(t) for t in templates]), strict=True
        )
    }
    print(f"embedded {len(templates)} templates in {time.perf_counter() - t0:.0f}s", flush=True)
    cache: dict[str, list[float]] = {}
    contents = []
    for title, head, parts, tags in specs:
        content = " ".join(s for _t, s in parts)
        src = memory_chunks.embed_source(title, content)
        chunks = memory_chunks.chunk_text(src)
        single, chunk_vecs = _approx_vectors(head, parts, chunks, src, tvec, nrng)
        cache[src] = single
        for c, v in zip(chunks, chunk_vecs, strict=True):
            cache[c] = v
        contents.append((title, content, tags))
    real_many = embeddings.generate_embeddings
    real_one = embeddings.generate_embedding

    def cached_many(items: list[str]) -> list[list[float]]:
        missing = [t for t in items if t not in cache]
        if missing:
            for t, v in zip(missing, real_many(missing), strict=True):
                cache[t] = v
        return [cache[t] for t in items]

    embeddings.generate_embeddings = cached_many
    embeddings.generate_embedding = lambda text: cached_many([text])[0]
    print(f"saving {notes} notes ...", flush=True)
    t1 = time.perf_counter()
    if ids:
        print(f"resuming after {len(ids)} saved notes", flush=True)
    try:
        for i, (title, content, tags) in enumerate(contents):
            if i < len(ids):
                continue
            rng = random.Random(seed * 1_000_003 + i)
            links = []
            if ids and rng.random() < 0.25:
                links = [f"extends:{rng.choice(ids)}"]
            kwargs = {"links": links} if links else {}
            try:
                out = mem.memory_save(
                    content=content,
                    title=title,
                    tags=tags,
                    author=rng.choice(AGENTS),
                    salience=round(rng.uniform(0.2, 0.8), 2),
                    **kwargs,
                )
            except TypeError:
                out = mem.memory_save(content=content, title=title, tags=tags, author="bench")
            ids.append(out["id"])
            if i % 250 == 0:
                print(f"  {i}/{notes} {time.perf_counter() - t1:.0f}s", flush=True)
    finally:
        embeddings.generate_embeddings = real_many
        embeddings.generate_embedding = real_one
    conn = storage.connect()
    try:
        counts = {
            "memory": conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
            "memory_embeddings": conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[
                0
            ],
            "memory_chunks": conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0],
            "memory_links": conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0],
        }
    finally:
        conn.close()
    marker.write_text(json.dumps(counts))
    print(f"store built in {time.perf_counter() - t0:.0f}s: {counts}")


def _load_model(threads: int) -> float:
    from unified_agent import config, embeddings

    if embeddings._model is not None:
        return 0.0
    t0 = time.perf_counter()
    if threads:
        from fastembed import TextEmbedding

        embeddings._model = TextEmbedding(model_name=config.embed_model(), threads=threads)
        embeddings._loaded_model_name = config.embed_model()
    else:
        embeddings.get_embedding_model()
    return time.perf_counter() - t0


class Legs:
    """Wraps the functions each leg calls and accumulates wall time per leg."""

    def __init__(self) -> None:
        self.times: dict[str, float] = defaultdict(float)
        self.undo: list = []

    def wrap(self, module, attr: str, leg: str, *, key=None) -> None:
        orig = getattr(module, attr)

        def timed(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return orig(*args, **kwargs)
            finally:
                name = key(args, kwargs) if key else leg
                self.times[name] += time.perf_counter() - t0

        setattr(module, attr, timed)
        self.undo.append((module, attr, orig))

    def reset(self) -> None:
        self.times = defaultdict(float)

    def restore(self) -> None:
        for module, attr, orig in reversed(self.undo):
            setattr(module, attr, orig)


class TimedConn:
    """Connection proxy that times the candidate-row hydration SELECT."""

    def __init__(self, conn, legs: Legs) -> None:
        self._conn = conn
        self._legs = legs

    def execute(self, sql, *args):
        if sql.startswith("SELECT * FROM memory WHERE"):
            t0 = time.perf_counter()
            rows = self._conn.execute(sql, *args).fetchall()
            self._legs.times["hydrate_rows"] += time.perf_counter() - t0
            return _Rows(rows)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Rows(list):
    def fetchall(self):
        return list(self)


def install_timers(legs: Legs) -> None:
    from unified_agent import (
        embeddings,
        memory_chunks,
        memory_graph,
        memory_knn,
        memory_ranking,
        recall_curation,
        storage,
    )
    from unified_agent.tools import memory as mem

    legs.wrap(embeddings, "generate_embeddings", "query_embedding")
    legs.wrap(embeddings, "get_embedding_model", "model_load")
    legs.wrap(memory_ranking, "lexical_leg", "lexical_fts")
    legs.wrap(mem, "_semantic_similarity_rows", "semantic_total")
    legs.wrap(
        memory_knn,
        "bounded_knn",
        "",
        key=lambda a, k: f"semantic_knn[{a[1]}]",
    )
    legs.wrap(memory_chunks, "max_similarities", "semantic_chunks_total")
    legs.wrap(mem, "_note_similarities", "semantic_exact_readback")
    legs.wrap(memory_graph, "graph_leg", "graph_leg")
    legs.wrap(mem, "_open_contradiction_counts", "contradictions")
    legs.wrap(recall_curation, "vote_rows", "votes")
    legs.wrap(recall_curation, "record_recall_hits", "recall_log_write")
    legs.wrap(mem, "_match_query_entities", "entity_match")
    orig_connect = storage.connect

    def connect(*a, **k):
        t0 = time.perf_counter()
        conn = orig_connect(*a, **k)
        legs.times["connect"] += time.perf_counter() - t0
        return TimedConn(conn, legs)

    storage.connect = connect
    legs.undo.append((storage, "connect", orig_connect))


def maxrss_mb(ru_maxrss: int, platform: str) -> float:
    """``ru_maxrss`` in MiB: the kernel reports bytes on macOS, kilobytes elsewhere."""
    return ru_maxrss / (1024.0 * 1024.0 if platform == "darwin" else 1024.0)


def peak_rss_mb() -> float:
    return maxrss_mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, sys.platform)


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    idx = min(len(s) - 1, max(0, round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def run_queries(n: int, legs: Legs, profile: bool) -> dict:
    from unified_agent.tools import memory as mem

    queries = (QUERIES * (n // len(QUERIES) + 1))[:n]
    walls: list[float] = []
    per_leg: dict[str, list[float]] = defaultdict(list)
    prof = cProfile.Profile() if profile else None
    diag: dict[str, int] = defaultdict(int)
    for q in queries:
        legs.reset()
        t0 = time.perf_counter()
        if prof:
            prof.enable()
        out = mem.memory_search(q, limit=10)
        if prof:
            prof.disable()
        wall = time.perf_counter() - t0
        walls.append(wall)
        accounted = 0.0
        for name, value in legs.times.items():
            per_leg[name].append(value)
        for name in (
            "query_embedding",
            "lexical_fts",
            "semantic_total",
            "semantic_exact_readback",
            "graph_leg",
            "hydrate_rows",
            "contradictions",
            "votes",
            "recall_log_write",
            "connect",
        ):
            accounted += legs.times.get(name, 0.0)
        # query_embedding nests inside nothing here; semantic_total excludes it.
        per_leg["fusion_and_rest"].append(max(0.0, wall - accounted))
        r = out["retrieval"]
        diag["semantic_matched"] += r["semantic"]["matched_count"]
        diag["lexical_matched"] += r["lexical"]["matched_count"]
        diag["graph_matched"] += r["graph"]["matched_count"]
    result = {
        "n": len(walls),
        "p50_ms": round(pct(walls, 50) * 1000, 1),
        "p95_ms": round(pct(walls, 95) * 1000, 1),
        "mean_ms": round(statistics.mean(walls) * 1000, 1),
        "legs_mean_ms": {
            k: round(statistics.mean(v + [0.0] * (len(walls) - len(v))) * 1000, 1)
            for k, v in sorted(per_leg.items())
        },
        "legs_p50_ms": {
            k: round(pct(v + [0.0] * (len(walls) - len(v)), 50) * 1000, 1)
            for k, v in sorted(per_leg.items())
        },
        "retrieval_mean": {k: round(v / max(1, len(walls)), 1) for k, v in diag.items()},
    }
    if prof:
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(35)
        result["profile"] = buf.getvalue()
    return result


def bench_vault(work: Path, notes: int, seed: int) -> dict:
    from unified_agent.tools import vault

    vroot = work / "vault"
    marker = vroot / f".bench-{notes}"
    rng = random.Random(seed + 1)
    names = [f"Note {i}" for i in range(notes)]
    if not marker.exists():
        for name in names:
            topic = rng.choice(list(TOPICS))
            folder = vroot / rng.choice(["Wiki", "Projects", "Areas", "Logs", "00 - Inbox"])
            folder.mkdir(parents=True, exist_ok=True)
            body = " ".join(rng.choice(TOPICS[topic]) for _ in range(rng.randint(3, 40)))
            links = " ".join(f"[[{rng.choice(names)}]]" for _ in range(rng.randint(0, 6)))
            (folder / f"{name}.md").write_text(
                f"---\ntags: [{topic}]\n---\n# {name}\n\n{body}\n\n{links}\n", encoding="utf-8"
            )
        marker.write_text("ok")
    out: dict[str, list[float]] = defaultdict(list)
    for q in QUERIES[:10]:
        t0 = time.perf_counter()
        vault.vault_search(q)
        out["vault_search"].append(time.perf_counter() - t0)
    paths = sorted(vroot.rglob("*.md"))[:10]
    for path in paths:
        t0 = time.perf_counter()
        vault.vault_links(str(path.relative_to(vroot)))
        out["vault_links"].append(time.perf_counter() - t0)
    return {
        k: {"p50_ms": round(pct(v, 50) * 1000, 1), "p95_ms": round(pct(v, 95) * 1000, 1)}
        for k, v in out.items()
    }


def bench_registered(n: int) -> dict:
    """Warm queries through the wrapper the hub registers, versus the bare call.

    The hub's latency numbers (``agent_perf_samples``) are taken by
    ``monitor.instrumented`` around the policy gate and the gateway's
    decision record, so they include more than ``memory_search`` itself.
    """
    from unified_agent import server
    from unified_agent.tools import memory as mem

    wrapped = server._guarded_and_instrumented(mem.memory_search)
    queries = (QUERIES * (n // len(QUERIES) + 1))[:n]
    bare: list[float] = []
    reg: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        mem.memory_search(q, limit=10)
        bare.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        wrapped(q, limit=10)
        reg.append(time.perf_counter() - t0)
    return {
        "bare_p50_ms": round(pct(bare, 50) * 1000, 1),
        "registered_p50_ms": round(pct(reg, 50) * 1000, 1),
        "registered_p95_ms": round(pct(reg, 95) * 1000, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", type=Path, default=Path.home() / ".cache" / "ua-bench-2444")
    ap.add_argument("--notes", type=int, default=3000)
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--seed", type=int, default=2444)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--vault", type=int, default=0, help="also bench a vault of this many notes")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument(
        "--no-vec",
        action="store_true",
        help="do not load sqlite-vec, as on a Python without extension loading",
    )
    ap.add_argument(
        "--registered",
        action="store_true",
        help="also time the hub's registered wrapper (policy, gateway, perf sample)",
    )
    ap.add_argument("--json", type=Path, help="write the result here as JSON")
    args = ap.parse_args()

    isolate(args.work.expanduser())
    build_store(args.work.expanduser(), args.notes, args.seed, args.threads)
    if args.build_only:
        return 0
    from unified_agent import embeddings

    if args.no_vec:
        import sqlite_vec

        def _refuse(_conn):
            raise RuntimeError("sqlite-vec disabled by --no-vec")

        sqlite_vec.load = _refuse
    legs = Legs()
    install_timers(legs)
    # Cold: the first query in a fresh process pays the model load.
    embeddings._model = None
    from unified_agent.tools import memory as mem

    legs.reset()
    t0 = time.perf_counter()
    if args.threads:
        legs.times["model_load"] += _load_model(args.threads)
    mem.memory_search("cold start query about deploy", limit=10)
    cold = {
        "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        "legs_ms": {k: round(v * 1000, 1) for k, v in sorted(legs.times.items())},
    }
    rss_after_cold = peak_rss_mb()
    warm = run_queries(args.queries, legs, args.profile)
    result = {
        "store": json.loads(
            store_marker(args.work.expanduser() / "home", args.notes, args.seed).read_text()
        ),
        "chunking": os.environ.get("UA_MEMORY_CHUNK_EMBEDDINGS", "default(on)"),
        "sqlite_vec": not args.no_vec,
        "cold": cold,
        "warm": {k: v for k, v in warm.items() if k != "profile"},
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "rss_after_cold_mb": round(rss_after_cold, 1),
    }
    legs.restore()
    if args.registered:
        result["registered"] = bench_registered(args.queries)
    if args.vault:
        try:
            result["vault"] = bench_vault(args.work.expanduser(), args.vault, args.seed)
        except Exception as exc:  # report it, do not lose the memory numbers
            result["vault"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2))
    if warm.get("profile"):
        print(warm["profile"])
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
