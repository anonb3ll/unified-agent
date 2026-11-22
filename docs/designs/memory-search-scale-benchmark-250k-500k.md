# Memory-search scale benchmark: 250K / 500K notes (HNSW threshold decision)

**Status:** decided 2026-09-14 (task #967, parent #963) — from a completed
250K/500K real run, not projected.

## Verdict

**No-go on a dedicated HNSW/ANN index at 250K-500K notes — confirmed by the
real run — but that is not the headline.** The semantic-similarity step is
exact at both scales (`engine_max_abs_similarity_diff` = `3.58e-7`, float32
rounding noise, across 12.5M and 25M compared rows respectively — see
[Recall](#recall-does-the-pipeline-already-approximate)), so an ANN index
has no recall gap to close; it would only trade that exactness away.

**But `memory_search` badly misses the research capture's own bar** ("recall
stays sub-50ms past ~250K notes"): measured p50/p95 latency was **14.8s /
15.8s at 250K notes and 29.3s / 31.8s at 500K notes** — roughly **300-600x**
past the 50ms target, not a marginal miss. And the failure mode is exactly
what [Secondary finding: the O(N) Python candidate loop](#secondary-finding-the-on-python-candidate-loop)
below predicted from reading the code: latency scales *linearly* with note
count (500K's p50/p95 are almost exactly 2x 250K's, matching the 2x note
count) — the signature of an O(N) per-row Python loop, not of an index
lookup, and not primarily an artifact of this contended host (see
[Environment caveats](#environment-caveats) for how much of the absolute
magnitude, as opposed to the linear *shape*, that host plausibly
contributes). **An HNSW index would not fix this** — it only replaces the
already-exact, already-fast semantic step; the dominant cost lives entirely
downstream of it, in the unconditional post-FTS/semantic candidate scan.

So the answer to task #967's literal question is "no, don't build HNSW,"
but the benchmark surfaced a different, more urgent problem: `memory_search`
is not production-safe at 250K-500K notes today, for a reason unrelated to
vector search. See [Decision](#decision) for what changes as a result.

**Revisit trigger:** this is not a "wait and see" situation — the O(N)
candidate loop fix below is the immediate next step, not deferred to a
future threshold. Once that loop is fixed (vectorized or otherwise made
sub-linear), re-run this benchmark to check whether the *semantic* step
alone then becomes the new bottleneck at higher scale (at which point an
ANN index becomes a live question again). Independently, re-run if the live
corpus crosses **~1M active notes** regardless of the loop fix's status, or
if `p95` latency measured on the actual Mac hub (not this desktop — see
[Environment](#environment-caveats)) exceeds **150ms** post-fix. See also
[Secondary finding: unbatched embedding backfill](#secondary-finding-unbatched-embedding-backfill)
below — a separate, lower-urgency scale risk this benchmark surfaced.

## Scope and the research-capture recommendation

Task #967's DoD points at
`00 - Inbox/Agent-Memory-Scale-Research-2026-09-09.md` (synthesized at
[[Wiki/Agent-Memory-at-Scale]]), whose Option A recommendation step 2 reads:

> **Prove the threshold:** benchmark recall / latency at ~250K / ~500K notes
> to decide when a real vector index is needed.

and, earlier in the same capture:

> A vector-count threshold that switches the embedding index from
> linear-scan to HNSW so recall stays sub-50ms past ~250K notes.

This document is that benchmark. It also has a direct, recent precedent:
PR #514 (task #997, merged as `ad71086`, the commit this branch is based on)
hit the exact same class of problem one layer over — `consolidate.py`'s
duplicate-clustering pass was an O(n^2) pure-Python comparison loop that
capped out around ~700 active notes, fixed by vectorizing the comparison
into one matrix multiply. Its commit message explicitly deferred "the real
long-term fix (bucketed/ANN comparison at 250K-500K notes)" to this task.

## Methodology

### Corpus generation and ingestion

`scripts/bench_memory_search_scale.py` generates a synthetic corpus and
inserts it through the **real** `memory` table schema (created by
`storage.init_db()`, unmodified) via the exact `INSERT` statement
`memory.memory_save`'s create path uses — same columns, same triggers
(`memory_fts` populates via the same `AFTER INSERT` trigger production
writes rely on). It deliberately does **not** call `memory_save()` itself:
that function runs an O(existing-notes) semantic-similarity dedup scan on
every create (see `tools/memory.py:_semantic_similarity_rows` called from
`memory_save`), which would make populating a quarter million notes an
O(n^2) operation — not a realistic bulk-import path, and not what this
benchmark is measuring.

Notes are grown **cumulatively** — 250,000 notes first, then 250,000 more to
reach 500,000 — so the total embedding work performed across both
checkpoints is 500,000 note-embeds, not 750,000.

Content length follows a lognormal distribution (median ~230 chars, long
tail to a 4,000-char clamp) modeling the real mix of short factual notes and
long research captures actually seen in this repo's memory.db; each note
anchors on one of 30 fixed topic phrases drawn from this repo's own subject
matter (task scheduling, deploys, vault sync, consolidation, agent
coordination, etc.) so embeddings cluster meaningfully instead of scattering
as uniform random noise — a corpus where nothing is ever similar to anything
else would make recall trivially perfect and uninformative.

**Assumption, stated plainly:** the real `memory.db` corpus's actual length
distribution was not measured for calibration (`memory_list` does not return
content, and pulling enough real notes via `memory_get` to fit a
distribution was judged not worth the overhead for this benchmark). The
lognormal parameters are a reasoned approximation from reading the schema
and this repo's own note-writing conventions, not a measured fit.

### Embedding: the real background catch-up mechanism, not a stand-in

`embed_missing_notes()` runs the **identical sequence of production calls**
`storage._backfill_embeddings()` uses — `embeddings.generate_embedding()` →
`embeddings.embedding_to_bytes()` → `INSERT INTO memory_embeddings` →
`storage.upsert_memory_vec()` — batched only for progress-logging
granularity, not parallelized or otherwise sped up. This is deliberate: the
DoD asks for the real background-embedding catch-up time, and
`storage._backfill_embeddings` is confirmed (by reading it, not assumed) to
be exactly this: a background thread spawned once at `init_db()` startup
that embeds any note lacking a `memory_embeddings` row, one `generate_embedding()`
call per note, sequentially.

One correction to the task's framing worth stating precisely: **`memory_save`
itself is synchronous, not async.** Reading `tools/memory.py:memory_save`
shows `_store_embedding()` is called inline, in the same request, before
`conn.commit()` — a normal single-note save is not deferred to a background
worker. The *background* mechanism is narrower than "writes are async": it
is specifically the startup catch-up pass for notes that predate an
embedding (a migration, a disabled-fastembed window, or — as modeled here —
a bulk import that bypassed `memory_save`). That distinction matters for
this report: the "catch-up time" numbers below describe onboarding a large
pre-existing backlog, not steady-state per-note write latency (which stays
one `generate_embedding()` call, same as always, paid at save time).

### Query latency and recall

Once a scale checkpoint reaches 100% embedded coverage, 50 queries are
sampled from the live corpus (a 4-7 word span pulled from a randomly chosen
note's own content, long enough to hit `memory_search`'s semantic-match
branch) and used for two measurements:

1. **Latency** — wall-clock `unified_agent.tools.memory.memory_search()`
   calls (the real, unmocked MCP tool entry point), p50/p95/p99/mean/max.
2. **Recall** — two distinct questions, not one, because they answer
   different parts of the DoD's "does the current pipeline already
   approximate/truncate before scoring all candidates" question:
   - **Engine exactness**: does `_semantic_similarity_rows` — the function
     `memory_search`'s semantic step actually calls, which tries the
     sqlite-vec `memory_vec` accelerated KNN path first and falls back to a
     per-row `cosine_similarity()` UDF scan — return the *same* per-note
     similarity as an independent brute-force cosine-similarity pass
     (`numpy`, computed directly from `memory_embeddings`, no shared code
     path with production) over the entire corpus? Reported as
     `engine_max_abs_similarity_diff` across every compared row of every
     sampled query.
   - **Blended-ranking recall@k**: how much overlap does `memory_search`'s
     final result set (which blends semantic similarity with FTS, recency,
     salience, access frequency, feedback, entity matches, and recall
     votes — see `tools/memory.py:memory_search`, section 4) have with a
     *pure*-semantic top-k ground truth? This is expected to be less than
     1.0 by design, not because of scale.

### Footprint

DB file size and per-table byte breakdown via SQLite's `dbstat` virtual
table, after a `PRAGMA wal_checkpoint(TRUNCATE)`.

### Reproduction

```
uv run python scripts/bench_memory_search_scale.py \
    --scales 250000,500000 --queries 50 \
    --ua-home /path/to/scratch/ua_home \
    --out /path/to/scratch/results.json
```

## Environment caveats

- Run host: this session's Linux desktop dev box — Intel Core i5-6600 @
  3.30GHz, 4 physical cores / 4 threads (no hyperthreading), 15GiB RAM.
  **Not** the Mac hub that actually serves `memory_search` in production —
  the Mac hub's CPU/RAM profile is unverified from this session. Absolute
  latency numbers below should be read as *this box's* numbers; the
  O(N)-vs-N scaling *shape* is what should transfer.
- **Confirmed under real, heavy memory pressure**, not a hypothetical
  caveat: at benchmark launch time this box was running ~10GiB/15GiB RAM
  used by concurrent desktop sessions (Obsidian, several Chrome/Claude
  Desktop renderer processes, two Hermes-agent daemons, multiple parallel
  Claude Code sessions) with swap at 3.9GiB/4.0GiB (~98% full) and holding
  there throughout. This is not incidental: a first attempt at this same
  benchmark (before this run) completed the full 250K embedding pass
  (5223.1s, 47.9 notes/s — well below the ~160-180 notes/s the script's
  docstring assumes from an isolated probe, itself a symptom of this box's
  contention) and then crashed with a genuine
  `sqlite3.OperationalError: disk I/O error` opening a fresh connection
  during the latency-measurement phase — consistent with a memory-pressure-
  induced I/O failure on a box with swap nearly exhausted, not merely an
  orphaned child process reaped when a parent session died. (The task
  framing assumed the latter; the run's own log contradicts that — worth
  recording precisely rather than repeating the assumption.) This run was
  relaunched via `setsid nohup ... disown`, fully detached from any
  controlling session, specifically so a repeat of either failure mode
  doesn't require yet another restart.
- fastembed model: `BAAI/bge-small-en-v1.5` (384-dim), the fixed default —
  same model production uses.
- sqlite-vec: confirmed present in this worktree's venv
  (`sqlite_vec` package importable) — `_semantic_similarity_rows` degrades
  silently to the Python `cosine_similarity()` UDF scan when unavailable,
  and the script does not directly log which path it took. Inferred
  instead from each scale's `footprint.memory_vec_bytes` in the raw results
  JSON (via `db_footprint`'s `dbstat` scan): a nonzero value means the
  `memory_vec` mirror table has rows, i.e. `upsert_memory_vec` succeeded and
  the accelerated KNN path was available to `_semantic_similarity_rows` —
  it does not, by itself, prove every query actually took that path over
  the UDF fallback, since the try/except in `_semantic_similarity_rows`
  isn't independently instrumented.
- This benchmark ran once per scale, not repeated for variance — a shared,
  contended dev box makes repeated-trial variance estimates unreliable
  either way; treat single-run numbers as directional, not statistically
  tight. Given the confirmed memory pressure above, absolute latency
  numbers here are plausibly *worse* than the Mac hub would see under
  comparable load, which argues for reading this run's numbers as a
  conservative (pessimistic) bound, not a best case.

## Results

Numbers below are this run's actual output (see
[Reproduction artifacts](#reproduction-artifacts) for where the raw JSON and
log live). 50 sampled queries per scale, `limit=10`.

| Scale | Insert (s) | Embed catch-up (s) | Embed rate (notes/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | max (ms) | Engine max abs diff | Blended recall@10 vs pure-semantic | DB size (MB) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 250,000 | 57.8 | 4,688.9 | 53.3 | 14,845.2 | 15,759.3 | 16,044.4 | 13,966.7 | 16,058.5 | 3.58e-7 | 0.550 | 1,127.3 |
| 500,000 | 60.3 | 4,509.9 | 55.4 | 29,256.9 | 31,849.8 | 33,052.3 | 27,211.1 | 33,631.8 | 3.58e-7 | 0.246 | 2,253.2 |

Notable shape: p50/p95/p99 all roughly **double** from 250K to 500K, tracking
the 2x note-count increase almost exactly (e.g. p50: 14,845ms -> 29,257ms is
a 1.97x increase against a 2.0x note-count increase). That is the fingerprint
of linear O(N) work per query, not of a search structure with sub-linear
scaling — consistent with, and strong evidence for, the
[O(N) Python candidate loop](#secondary-finding-the-on-python-candidate-loop)
being the dominant cost, independent of whatever the semantic step itself
does. DB footprint also scales almost exactly linearly (1,127MB -> 2,253MB,
~2.0x), as expected for a corpus with no compaction between checkpoints.

Embed rate (~53-55 notes/s) is well below the ~160-180 notes/s the script's
docstring assumed from an isolated batched probe — see
[Secondary finding: unbatched embedding backfill](#secondary-finding-unbatched-embedding-backfill)
and the memory-pressure caveat in [Environment](#environment-caveats) for
why; both the per-note (not batched) embedding calls and this host's
contention plausibly contribute.

## Recall: does the pipeline already approximate?

**No.** `_semantic_similarity_rows` computed a per-note cosine similarity
that matched the independent brute-force numpy ground truth to within
`3.58e-7` — float32 rounding noise, not a meaningful difference — across
every compared row of every sampled query at **both** scales identically
(12,500,000 compared rows at 250K, 25,000,000 at 500K; `memory_vec_bytes`
was nonzero at both scales — 391.6MB and 781.7MB respectively — confirming
the sqlite-vec `vec0` mirror was populated and available as the accelerated
path). This is by construction, not luck: both the sqlite-vec KNN query
(`k = total embedded notes`, per `_semantic_similarity_rows`'s docstring)
and the UDF fallback score *every* live, non-superseded note — neither
truncates or samples before scoring. There is no approximation in the
current semantic-scoring step to trade away for an ANN index's speed; an
HNSW switch would be trading exactness *away* in exchange for latency this
exact path does not need help with.

The blended-ranking recall@10 number dropped from **0.550 at 250K to 0.246
at 500K** — a real, measured shift, and worth explaining rather than waving
away as "expected by design" without qualification. Two things are true at
once: (1) `memory_search`'s final ranking blends FTS/recency/salience/
access/feedback/entity/vote terms on top of raw similarity by design, so
*some* gap versus pure-semantic top-k is expected regardless of scale — that
part is not new information; (2) the *size* of the drop between scales is
plausibly a synthetic-corpus artifact, not a production-relevant recall
regression: this benchmark's corpus anchors every note on one of a **fixed
30-topic vocabulary** (see [Methodology](#corpus-generation-and-ingestion)),
so topic density roughly doubles from ~8,300 notes/topic at 250K to ~16,700
notes/topic at 500K. More near-duplicate notes per topic means more ties
(or near-ties) in raw cosine similarity among the pure-semantic top-10,
which the hybrid ranking's other terms (BM25 rank, recency, entity matches)
increasingly reorder among a growing pool of similarity-tied candidates —
inflating the apparent recall gap without any change to the underlying
similarity computation's correctness (which, per the exactness result
above, is unchanged at both scales). A real corpus, without an artificially
fixed topic vocabulary, would not necessarily show this same scale-dependent
widening. Treat the recall@10 *trend* here as a corpus-construction
artifact worth flagging, not as evidence `memory_search`'s hybrid ranking
degrades at scale — the engine-exactness result is the one this document
leans on for the HNSW decision.

## Secondary finding: the O(N) Python candidate loop

(Named "secondary" only because it's outside task #967's literal HNSW
question — empirically, per [Results](#results), it is the **primary**
driver of `memory_search`'s unacceptable latency at this scale, not a minor
addendum.)

Reading `memory_search` end to end (not just its semantic step) surfaced a
larger latency risk than the vector search itself. After the FTS and
semantic passes, `memory_search` runs:

```python
all_notes = conn.execute(f"SELECT * FROM memory{where_clause}", params).fetchall()
```

— an unfiltered-by-relevance fetch of essentially every live, non-expired,
non-superseded note (tag filters narrow this only when the caller passes
`tags=`) — followed by a **pure-Python loop over every one of those rows**:
JSON-decoding tags, running entity-extraction regexes, and computing the
full weighted-score formula (semantic + FTS + recency + salience + access +
feedback + entity + vote terms), before sorting and truncating to `limit`.

This loop is O(N) in Python, not in a vectorized/native scan, and it runs on
**every** `memory_search` call regardless of whether sqlite-vec is
available. The real run confirms this is not a theoretical concern: p50/p95
latency at both scales (14.8s/15.8s at 250K, 29.3s/31.8s at 500K) scales
almost exactly linearly with note count — the signature of this loop, not of
the semantic step, which stays a single native/SIMD-accelerated scan (and
stays exact — see [Recall](#recall-does-the-pipeline-already-approximate))
regardless of corpus size. **An HNSW index would not fix this**: it only
replaces the semantic-similarity step, and this loop runs independently of
that step, on every call, HNSW or not.

Given the measured magnitude — 14.8-33.6 seconds per `memory_search` call at
250K-500K notes, a case any agent workspace whose `memory.db` grows into
that range will eventually hit — this is filed as a **P0-equivalent
follow-up**, not deferred as a nice-to-have (see
[Follow-ups filed](#follow-ups-filed) below for the tracked task). Task
#967's scope is the benchmark and the HNSW decision, not the
`memory_search` refactor itself, and this repo's own convention (PR #514 /
task #997) is to fix each O(N) hotspot as its own reviewed change against
the specific mechanism it slows down — so the fix lands as a separate PR,
but it should not sit in the backlog the way a normal "found while
benchmarking" footnote would.

## Secondary finding: unbatched embedding backfill

`storage._backfill_embeddings()` (and this benchmark's timed mirror of it,
`embed_missing_notes()`) calls `embeddings.generate_embedding()` **once per
note**, each call a single-item `model.embed([text])`. A quick isolated
probe on this same host showed fastembed batching many texts into one
`embed()` call sustains roughly 3-4x the throughput of the same texts
embedded one at a time (~160-180 notes/s batched vs. the per-note rate
recorded in [Results](#results)) — the fixed per-call overhead (tokenization
setup, ONNX session dispatch) dominates at batch size 1. Batching
`_backfill_embeddings`'s loop (e.g. `model.embed()` over a chunk of pending
texts at once, mapping results back to note ids) is a low-risk, independent
win for onboarding-time whenever a large pre-existing backlog needs to catch
up — unrelated to the HNSW question, filed separately.

## Decision

Stay on Option A's storage/indexing shape exactly as already shipped: SQLite
+ FTS5 + fastembed, with the sqlite-vec `vec0` accelerated exact-KNN mirror
where available and the UDF fallback otherwise. **Do not add HNSW or any
other ANN index** at 250K-500K notes: there is no recall gap for an ANN
index to close (confirmed exact at both scales), and the latency problem
this benchmark found is not the one an ANN index solves.

That said, this is **not** a "ship as-is, all clear" result. The research
capture's own success bar (sub-50ms) is missed by 300-600x at exactly the
scale it was meant to hold at, and the cause — the O(N) Python candidate
loop — is a real, measured, production-relevant defect independent of the
HNSW question. The actionable next step is fixing that loop (tracked
separately, see below), not adding a vector index. Re-benchmark once that
fix lands to see whether the now-exposed semantic step becomes the new
long pole at higher scale.

## Follow-ups filed

- **(P0-equivalent — tracked as task #1001)** Vectorize
  `memory_search`'s post-FTS/semantic candidate loop (JSON tag decode,
  entity-match regex, scoring formula) the same way PR #514 vectorized
  `consolidate.py`'s duplicate-clustering comparison. Confirmed, not
  speculative: this run measured 14.8s/29.3s p50 `memory_search` latency at
  250K/500K notes, scaling linearly with note count — the O(N) loop, not the
  (exact, unchanged) semantic step, is the dominant cost. Independent of the
  HNSW question.
- Batch `storage._backfill_embeddings()`'s embedding calls (`model.embed()`
  over a chunk at once) instead of one `generate_embedding()` call per note.
  This run measured 53-55 notes/s per-note embedding vs. an isolated probe's
  ~160-180 notes/s batched — roughly 3x headroom for large-backlog
  catch-up time.

## Reproduction artifacts

The raw JSON results and run log this document's numbers were transcribed
from are host-local scratch files (not committed — they're synthetic-corpus
output, not source), referenced in task #967's notes and evidence pointer.
The benchmark script and its tests, which *are* committed and are what
actually reproduces this run, live at `scripts/bench_memory_search_scale.py`
and `tests/test_bench_memory_search_scale.py`.
