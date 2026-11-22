# Memory, tasks, and vault: the persistent-state layer

Three interlocking pieces of shared, cross-host state, all backed by the SQLite database
at `UA_HOME/memory.db` (default `~/.unified_agent/memory.db`) plus, optionally, an
Obsidian vault at `UA_VAULT_PATH`. Tool signatures and limits are in
[tools-reference.md](tools-reference.md#memory-toolsmemorypy); this page covers the
conventions for using them well.

## Memory

Persistent notes (`memory` table, full-text via SQLite FTS5, plus local `fastembed`
vector embeddings for semantic search) shared across every host/session pointed at the
same `UA_HOME` — a note saved from Claude Code is immediately visible from Antigravity,
another machine, or the TUI.

### Tags

Taxonomy used throughout the project:

- `project:<name>` — which project a note concerns.
- `pref` — a user preference.
- `fact` — a stable technical fact.
- `decision` — a choice and its rationale.

Always tag saves; untagged notes still work (they're bucketed under an internal
`untagged` label for indexing purposes) but are harder for other agents to discover via
`memory_list(tag=...)`.

### Author labels and the overwrite guard

Pass `author=<your label>` (e.g. `agent-claude`, `agent-laptop-agent3`) on every `memory_save`
call — it's recorded as the note's owner (or, on update, as `updated_by`) so peers can
see who to attribute or ask. Because the database is shared across agents and machines,
`memory_save` enforces a guard at the code level (see
[security.md](security.md#memory-cross-author-overwrite-guard)):

- A **full-replace update** (`append=False`, the default) of a note owned by a
  *different* author — or any owned note when the caller passes no `author` at all — is
  refused with a `ToolError` unless `overwrite=True` is passed explicitly.
- **`append=True`** is the safe alternative: it appends your content as an attributed
  block (`\n\n---\n*[author · timestamp]*\n\n{content}`) without touching what's already
  there, and requires an existing `id`. The update reads and writes the note under
  one write lock (`BEGIN IMMEDIATE`), so two concurrent appends both land (task #1914).
- **`overwrite=True`** destroys the peer's prior content. Reserve it for the note's
  actual owner correcting their own note, or for the user's explicit direction — this is
  the standing rule "never overwrite work not meant to be deleted" enforced in code, not
  just convention.
- On an update, omitting `title`/`tags` **preserves** the existing values; pass `tags=[]`
  explicitly if you want to clear tags (an omitted `tags` is not treated as "clear").

```json
{"content": "Ship blocked on migration 0042 review.", "id": "8bd66e90712c", "author": "agent-claude", "append": true, "tags": ["decision"]}
```

The note's embedding is computed before the write lock is taken, never inside the
write transaction (task #1914), so a cold model load cannot hold the hub's write
lock. If the note changed between that read and the locked write, or the model is
unavailable, the save drops the stale vector and the background backfill
(`memory_semantic.backfill_memory_embeddings`) embeds the committed text later.

### Search

`memory_search` is hybrid: SQLite FTS5 `bm25` plus cosine similarity over
`fastembed` vectors (`memory_embeddings` table), fused by rank (task #1913). If
`fastembed` can't load (cold cache with no network, or the model genuinely
unavailable), search degrades to the lexical and identifier legs; nothing errors.
`memory_list` is a plain newest-first browse, optionally filtered to one tag.

**Retrieval diagnostics (#1955).** Every `memory_search` response adds a
`retrieval` envelope alongside the existing `results`, including empty results:

```json
{
  "version": 1,
  "mode": "hybrid",
  "lexical": {"status": "queried", "matched_count": 0},
  "semantic": {"status": "available", "matched_count": 0, "failure_category": null}
}
```

`mode` is `hybrid` when query embedding and semantic lookup complete, even when
no eligible note meets the semantic similarity floor or no stored embeddings
exist. On the existing semantic failure fallback, it is `lexical_fallback`,
with semantic `status: "unavailable"` and `failure_category: "model_acquisition"`
or `"runtime"`. Acquisition uses the shared phase-aware classifier: a recognized
transport/cache failure during model construction qualifies; inference errors,
missing required imports and other defects are `runtime`. Categories contain no
exception text, paths or credential values. Fallback preserves lexical and
identifier matches and does not change ranking or eligibility.

`matched_count` counts each bounded leg's eligible matches before the response
limit, not the corpus or the returned-result total. Semantic matches must also
meet the similarity floor and top-K cap. Counts exclude other tenants and notes
excluded by the request's existing tag, host, expiry and note-state filters.
Lexical `status: "queried"` means the existing FTS helper was called; it does
not certify FTS health because that helper preserves its SQLite-error-to-empty
fallback. These diagnostics describe retrieval availability, not relevance or
memory completeness.

**Recalled notes are untrusted (#1952).** `memory_get` and every
`memory_search` hit carry an additive
`trust: {level: "untrusted", source: "memory", author?, note}` field — the
same envelope agent messages and vault inbox reads already use. Existing
keys and the result shape are unchanged. The field tells the reading model
the text is **data, not instructions**. Every note is labelled, including
the caller's own: a stored `author` is a claim (an unverified stdio save
may name anyone; a peer's `append` keeps the owner's `author`), so no
stored field proves who wrote the text. This label is not the eligibility
`trust` / confidence score used when ranking packets, and it does not
sanitize the body.

**Candidates.** A note is a candidate if any content leg matches it:

- **Lexical.** An FTS5 AND of every query token runs first. When it finds fewer
  than `UA_MEMORY_FTS_OR_THRESHOLD` (10) notes, an OR of the query's
  non-stopword tokens adds partial matches. Both queries are ordered by bm25 and
  capped at `UA_MEMORY_FTS_LIMIT` (200) rows. AND hits always rank ahead of
  OR-only hits. Before #1913 the query was AND-only, so a natural-language
  query with one word absent from the note matched nothing.
- **Semantic.** Notes with cosine similarity of at least
  `UA_MEMORY_SEMANTIC_MIN_SIMILARITY` (0.65), best first, capped at
  `UA_MEMORY_SEMANTIC_TOP_K` (50). This applies to queries of any length; the
  old gate (0.55, three or more words) skipped one- and two-word queries and let
  in bge-small's 0.55-0.60 band for unrelated text. A note longer than about
  2,000 characters can also carry overlapping chunk vectors when
  `UA_MEMORY_CHUNK_EMBEDDINGS` is on (default on, task #2182). The note's
  semantic score is the best of its single vector and those chunks. Short
  notes are unchanged. See [Embedding model](configuration.md#embedding-model).
  With sqlite-vec loaded, the leg reads the `memory_vec` (and
  `memory_chunk_vec`) mirror through a bounded KNN (task #2214):
  `k` starts at 64, or twice the top-K, and doubles until the cut provably
  holds every note the leg can use. The proof is one of three: the mirror is
  exhausted; the last row read is below the similarity floor; or top-K
  eligible notes are in hand and the last row read scores strictly below the
  top-K-th of them. Eligibility (tenant, deleted, superseded or `as_of`
  validity, tags, host, expiry) is applied after each cut, so superseded,
  deleted and foreign rows never take a slot in the result. The leg's notes
  are the same as a full scan's. A lexical, identifier or graph hit outside
  the cut still reports its exact similarity in `explanation`. sqlite-vec
  refuses a `k` above 4096. Each vec0 query is itself a flat scan of
  the mirror, so the loop has a budget: at most three native queries;
  with a top-K, one query in which no eligible note appears ends the loop;
  and the next `k` is sized from the eligible yield seen so far. When the
  budget runs out, the per-row scan below answers. A call can therefore run
  up to three native scans before that full scan, and only one when its first
  cut finds no eligible note. A search calls it once per mirror, so with
  chunking on the single-vector and chunk legs each have that budget. With
  chunking on, the chunk cut stops at the top-K-th
  note's single-vector score, since a weaker chunk cannot lift a note into the
  leg. Before #2214 the query asked for
  `k = COUNT(*)`, so a store with more than 4096 vectors fell back to the
  per-row `cosine_similarity()` scan on every search. That scan is still the
  fallback, used when 4096 rows cannot prove the cut. `memory_save`'s dedup
  check uses the same loop with `UA_MEMORY_DEDUP_THRESHOLD` (0.85) as its
  floor.
- **Identifier.** The query names a task (`#123`, `task 123`) or branch
  (`task/123-slug`) found in the note's title, content, tags, host or author.
- **Graph (task #2096).** One hop from the best lexical and semantic hits over
  memory links and shared identifiers, so a note linked to a hit is found
  even when the query never mentions it. See
  [Memory graph](#memory-graph-task-2096).

**Fusion.** Each leg is ranked on its own: lexical by bm25, semantic by
similarity, entity by matched project/task/branch/agent/service identifiers,
recency by whole days since `updated_at`, graph by hop strength (below).
Tied values share a rank. The fused
score is reciprocal rank fusion,

```text
rrf = sum over legs the note appears in of  w_leg / (k + rank_leg)
combined_score = rrf * prior
```

with `k = UA_MEMORY_RRF_K` (60) and leg weights `UA_MEMORY_WEIGHT_FTS` (1.0),
`UA_MEMORY_WEIGHT_SEMANTIC` (1.0), `UA_MEMORY_WEIGHT_ENTITY` (1.0) and
`UA_MEMORY_WEIGHT_RECENCY` (0.2), plus `UA_MEMORY_WEIGHT_GRAPH` (0.5) for the
graph leg. A weight of 0 drops the leg. The first four variables keep their
names but now scale a rank contribution, not a raw score, and their defaults
changed (they were 0.5, 1.0, 0.6 and 0.3 in the linear sum).

**Prior.** Quality signals adjust the fused score by a multiplier clamped to
[0.8, 1.2], limiting adjustments to 20 percent in either direction. These
bounded priors can reorder matches; they do not guarantee relevance-order
dominance. Access and automatic recall counts no longer add popularity boosts:

```text
p = w_salience * 2 * (salience - 0.5) + w_feedback * 2 * (confidence - 0.5) + w_vote * vote
prior = clamp((1 + 0.2 * tanh(p)) * age_discount, 0.8, 1.2)
```

| Input | Value | Weight (default) |
|---|---|---|
| Salience | stored importance, 0.0 to 1.0; the 0.5 default is neutral | `UA_MEMORY_WEIGHT_SALIENCE` (0.5) |
| Confidence | effective confidence (task #2097): the Beta posterior from `memory_feedback` counts, lowered by open contradictions; 0.5 (no feedback, no contradiction) is neutral. See [Evolving confidence](#evolving-confidence-and-note-versions-task-2097) | `UA_MEMORY_WEIGHT_FEEDBACK` (0.4) |
| Vote | `recall_curation.vote_term(0, upvoted)`: explicit "helpful" upvotes only | `UA_MEMORY_WEIGHT_VOTE` (0.35) |
| Age discount | 1 until `UA_MEMORY_DECAY_MONTHS` (6), then `0.95` per extra month, floored at 0.5 | — |

**Not ranked.** `access_count` and the automatic `recalled_count` are usage
statistics only. Every returned note still bumps both (`memory_get`,
`recall_maintenance` and the UI read them), but neither feeds the score: they
measure what search already returned, so ranking on them let popular notes stay
on top whatever the query. `UA_MEMORY_WEIGHT_ACCESS` is deprecated and ignored.

**Explanation.** Each result's `explanation` carries `ranks` (per-leg rank, or
`null` when the note is not in that leg), `rrf`, `prior`, `prior_inputs`
(`salience`, `confidence`, `vote`: each prior's signed input),
`confidence_posterior` (task #2097),
`lexical_mode` (`and`, `or` or `null`), the raw signals (`lexical`, `semantic`,
`recency`, `salience`, `access`, `feedback`, `entity`, `vote`; `feedback` is
`2 * posterior - 1`, the feedback-only signal), `host`,
superseded/expired flags, `matched_tags`, `entity_matches`, `graph` (`null`
or `{score, via}`), and `why`, the ordered reasons. The graph lives in the same
`memory.db`, in two tables; there is no second graph store.

**Evaluation.** `scripts/eval_memory_1913.py` scores ranking on a frozen
synthetic fixture (`tests/fixtures/memory_eval_1913/corpus.json`: 168 notes, 50
labelled queries covering paraphrase, identifiers, partial multi-word, one- and
two-word, popular-but-irrelevant, superseded and recent-vs-old cases) and prints
recall@5, recall@10 and MRR per category. `--lexical` runs without the
embedding model. `tests/test_eval_memory.py` holds the quality floor.
Change the fixture only by adding a new version. On 2026-10-06:

| Mode | Ranking | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| lexical | linear sum, AND-only FTS | 0.38 | 0.38 | 0.37 |
| lexical | RRF (#1913) | 0.94 | 0.94 | 0.83 |
| hybrid | linear sum, AND-only FTS | 0.48 | 0.58 | 0.42 |
| hybrid | RRF (#1913) | 1.00 | 1.00 | 0.93 |
| lexical | RRF + graph leg (#2096, 2026-10-15) | 0.94 | 0.94 | 0.84 |
| hybrid | RRF + graph leg (#2096, 2026-10-15) | 1.00 | 1.00 | 0.93 |

Excluded-note leaks are 0 in every #2096 run.

**Public benchmark.** `scripts/eval_longmemeval.py` runs LongMemEval-S against
production `memory_search` and `vault_query`, with a fresh temporary store per
question. The scorecard, with its settings, per-type scores, misses and rerun
commands, is [evals/longmemeval.md](evals/longmemeval.md) (task #2145).

Update stale notes via `memory_save(id=...)` rather than creating near-duplicates;
delete notes that turn out to be wrong (`memory_delete`) rather than leaving them to
confuse future search results. `memory_delete` is owner-or-human and soft: the
row stays as a tombstone tagged `meta:deleted`, hidden from `memory_list` and
`memory_search`, and still readable with `memory_get`.

### Automatic session context

Session-start/resume packets derive a bounded memory section from the task title
(task #1621). Candidates are an FTS5 OR of up to eight title words of three or
more letters, stopwords dropped (task #1913; it was an AND of every word, which
rarely matched a real title). A note with `project:` attribution is eligible
only when one of its project tags matches the task project. Matching ignores case/outer whitespace and
uses the existing checkout-to-project mapping (`unified_agent` → `unified-agent`,
`tap` → `client-portal`, `sample-store` → `sample-store`). Multiple project tags
explicitly share a note across those projects. Legacy notes without project tags
remain global context, ranked after project matches; malformed attribution is
excluded. Scope uses all stored tags before the packet's display truncation.

Memory eligibility also requires its stored `org_id` to match
`edition.tenant_org_id()`, and the task's stored organization must match that same
configured organization. NULL represents the private/core hub scope, never a
wildcard for tenant rows. Organization-unattributed legacy rows therefore stay in
the private scope and are excluded from configured enterprise tenants; this reader
does not migrate or reinterpret them. Project, organization, confidence, trust and expiration eligibility precede the
candidate limit so ineligible matches cannot crowd out eligible notes. Candidates
are ordered by project scope, then bm25, then the same finite-confidence/salience
fallback as the packet reader.

Existing confidence, trust, expiration, supersession, item/token and packet-byte
limits still apply. No task or no searchable title yields no automatic memory.
Restart re-reads the canonical store. Packets remain historical context with
`authority: false`; injected notes grant no permissions or approval.

This protects the automatic **memory section**. It does not add per-session tenant
authentication or authorize/filter the packet's other sections. The configured
organization is a deployment seam, not a new per-caller tenant identity.

Explicit memory tools apply the same fence (task #1762): `memory_get`,
`memory_search` (FTS snippets, semantic hits and identifier matches),
`memory_list`, `memory_restore`, `memory_delete`, `memory_feedback`,
`memory_promote`, `memory_prune_candidates`, `recall_maintenance`, the save-time
dedup `similar` list and the `memory` platform resource all filter on
`org_id IS tenant_org_id()`. A note in another tenant answers exactly like an id
that does not exist (`no memory note with id ...`, or `{deleted: false}`), so the
tools are no existence oracle.

### Hard deletion and legacy auxiliary cleanup (task #1815)

Index-note cleanup and migration-import rollback hard-delete notes through
`storage.hard_delete_memory`. It checks the same tenant fence before touching
auxiliary rows, deletes the vector mirror and recall votes before removing the
note, and leaves commit/rollback to its caller. Source embeddings, and the
note's `memory_entities` and `memory_links` rows (task #2096), cascade through
their foreign keys. A failed cleanup rolls back its savepoint; an absent
optional vector table is supported, while a present but unusable table prevents
hard deletion. `memory_delete` remains a soft delete.

For an initialized store with historical orphans, the local maintenance function
`storage.repair_memory_orphans(conn)` returns a preview by default. It removes no
notes or source embeddings. Besides vector rows with no note, it identifies stale
vectors whose reused rowid now points at a note with no matching source embedding.
Recall votes are removed only when their note no longer exists. Valid auxiliary
rows from every tenant remain intact.

After this change is deployed, a maintainer can preview from the serving checkout
using its normal runtime environment:

```python
from unified_agent import storage

conn = storage.connect()
try:
    print(storage.repair_memory_orphans(conn))
finally:
    conn.close()
```

Applying a reviewed, explicitly authorized one-off repair uses
`storage.repair_memory_orphans(conn, apply=True)` followed by `conn.commit()`;
closing or rolling back instead discards it. This is a local maintenance function
across the store, not a tenant-facing MCP tool, startup hook or automatic migration.
Its result reports `memory_vec`, `memory_recall_votes` and `applied`. A repeat repair
reports zero removals once the store is consistent.

### Evidence-backed notes and experience (task #1459)

Follows the #1458 verdict: Hindsight's retain/recall/reflect ideas are adapted
inside the one `memory.db` authority, not added as a second store.

- **Fact vs inference.** `memory_save(epistemic="fact"|"inferred")` labels a
  note. An inferred note must carry `evidence`: `<kind>:<id>` refs (approval,
  commit, delegation, event, memory, pr, run, session, task, url, vault), at
  most 20. Evidence accumulates on update unless the owner passes
  `evidence_replace=True`; `contradicts=[]` clears links. A peer may add refs
  but not change the owner's `epistemic`, `valid_from`, `contradicts` or
  existing evidence without `overwrite=True`.
- **Temporal validity.** `valid_from` records when the claim became true;
  a labelled note (`epistemic` or `evidence` set) defaults it to creation time. `expires_at` is still the end of a TTL.
- **Bi-temporal validity (task #2095).** `valid_to` records when the claim
  stopped being true. A supersede (`memory_save(supersedes=...)` or the
  consolidate apply step) invalidates rather than deletes: it stamps the old
  note's `valid_to` with the replacement's `valid_from`, or the supersede time
  when the replacement has none or its `valid_from` is not after the old
  note's own start. An empty or inverted interval is never written: a note
  superseded within the second it was saved ends one microsecond after its
  start, and superseding a note whose `valid_from` is still in the future is
  refused (give the replacement a later `valid_from`, or correct the old
  note's first). The supersede is refused when the note is already
  superseded, is fenced to the caller's tenant, and records its actor in
  `agent_events` in the same transaction, so it cannot commit without one. `memory_restore` and
  `unified-agent-consolidate restore` clear `valid_to` with the pointer.
  `memory_search(as_of=...)` and `memory_get(as_of=...)` take an ISO datetime
  with a UTC offset (a naive time is refused, as `task_list(created_since=...)`
  does) and answer for that instant: a note qualifies when
  `(valid_from IS NULL OR valid_from <= as_of) AND (valid_to IS NULL OR as_of < valid_to) AND created_at <= as_of`,
  so a note superseded later is returned, by the lexical and the semantic leg
  alike. Bounds compare at microsecond precision, identically in search and
  get. One `as_of` covers both axes: a
  replacement saved today with a back-dated `valid_from` is not visible at an
  earlier `as_of`, because the hub did not hold it then. A note superseded
  before `valid_to` existed has no recorded end and stays hidden under
  `as_of`, as it is without it. Expiry and recency are measured at `as_of`;
  the content returned is the note's current text; prior texts are in
  `memory_get(include_versions=True)` (task #2097).
  Without `as_of`, search and get behave as before. The recency leg dates a
  note by `valid_from` when set, else `updated_at`. The Control Center
  `/api/memory?q=...&as_of=...` passes `as_of` through to `memory_search`.
- **Changed facts.** `memory_save(supersedes=<id>)` creates the replacement
  and points the old note's `superseded_by` at it in one transaction, so the
  stale answer leaves `memory_search`, session packets and the tag/master
  index notes but stays readable
  through `memory_get` (which lists the reverse link as `supersedes`) and is
  reversible with `memory_restore`. Superseding another author's note is
  refused without `overwrite=True`, a broadcast note stays human-only, and a
  note outside the caller's tenant reads as missing, for `supersedes`,
  `contradicts` and update by `id` alike.
- **Contradictions.** `contradicts=[<id>, ...]` links notes this one disputes
  without hiding them, so the owner can resolve it.
- **Links.** Supersedes, contradicts and `memory:<id>` evidence refs are also
  recorded as typed links; see [Memory graph](#memory-graph-task-2096).
- **Packet labels.** Injected memory hits carry `epistemic` (`unlabeled` for
  legacy notes), `author`, up to five `evidence` refs and `valid_from`. These
  labels count against `UA_MEMORY_INJECT_TOKEN_CAP` with the title and snippet.
- **Experience.** The packet's `experience` section lists up to three ended
  sessions (`completed`, `failed`, `abandoned`) on the same task, excluding the
  caller's own session: agent, status, exit status, end time, a redacted
  summary marked `summary_source: agent_reported`, and `session:`/`task:`/
  `delegation:` evidence. Records are `epistemic: fact` because the hub
  observed them. Agent-supplied text (agent label, exit status, summary) is
  redacted and length-capped. `comparable_packet()` omits `experience`
  because it excludes the caller's own session. A task with any ended session
  reserves half of `UA_MEMORY_INJECT_TOKEN_CAP` for experience, before excluding
  the caller; memory hits use the other half. This keeps memory selection
  identical across callers even when summaries have different costs or none
  of the caller's prior outcomes fit. Tasks without ended sessions retain the
  full memory budget. The whole injected context stays within the cap.
  Tenant eligibility matches the memory section.

### Memory graph (task #2096)

Epic #1915 M6, after Supermemory's relations and Hindsight's graph leg, kept
inside `memory.db`. Two tables:

| Table | Rows | Written by |
|---|---|---|
| `memory_entities(note_id, kind, value, org_id)` | the task, branch, project, service and agent identifiers a note names, casefolded, at most 64 per note | every `memory_save` (rewritten on update from the final title, content and tags); cleared by `memory_delete` |
| `memory_links(src_id, dst_id, rel, actor, created_at, org_id)` | typed edges, `rel` one of `supersedes`, `contradicts`, `extends`, `derives`, `evidence` | see below |

Links are written in the save's own transaction:

- `supersedes`: `memory_save(supersedes=<old>)` and consolidate apply write
  `new -> old`; `memory_restore` and consolidate restore remove it with the
  pointer.
- `contradicts`: mirrors the `contradicts` column (`note -> disputed`). An
  update that passes `contradicts` replaces them.
- `evidence`: one per `memory:<id>` evidence ref whose target is a note in the
  same tenant (`note -> cited`). Other ref kinds are not notes.
- `extends`, `derives`: explicit, `memory_save(links=["extends:<id>", ...])`.
  Targets must be live notes in the caller's tenant; on update the list
  replaces them and `links=[]` clears them; a peer's links need `overwrite=True`.

The extractor is the one `memory_search` runs on a query
(`_extract_query_entities`). `actor` is the saving agent; `created_at` is the
save time. Both tables cascade when a note is hard-deleted
(`storage.hard_delete_memory`).

**Backfill.** The `tools.memory.graph_backfill` post-init hook (foreground,
order 40) walks `memory` by rowid from a cursor in `meta`
(`memory_graph_backfill_rowid`). Each write batch covers at most 16 notes
and 256 KiB of snapshot text; one oversized note is processed alone. Reading,
entity extraction and JSON parsing run outside the write lock. Legacy BLOB
bodies are decoded as UTF-8 with replacement for invalid bytes only for entity
extraction; stored notes are unchanged. Rows with non-text IDs are skipped
without coercing or rewriting their identities, and the cursor advances past
them so later notes are still processed. Relation preparation uses only the
first 20 entries of each persisted `contradicts`/`evidence` array, matching the
public cardinality cap and bounding insert probes even for malformed rows.
After taking
`BEGIN IMMEDIATE`, the backfill checks the cursor and SQLite's `data_version`.
If another connection committed, it compares the exact source rows before
writing. Changed snapshots retry, including changes that retain `updated_at`;
unrelated writes allow progress. Graph rows and the advanced cursor commit
together. Per note it replaces
the derived rows: entities (none for a deleted note), the `contradicts` and
`evidence` links from those columns, and the incoming `supersedes` link from
`superseded_by`; explicit extends/derives links are untouched. A re-run or a
reset cursor converges on the same rows and repairs stale ones. Backfilled links are dated by the asserting note's
`created_at` (a supersede by `valid_to` when recorded).

**Graph leg.** The seeds are the top `UA_MEMORY_GRAPH_SEEDS` (5) notes by
reciprocal rank over the lexical and semantic legs. From each seed the leg
walks one hop:

- links in either direction, weight 1 per link;
- notes sharing an entity, weight `kind_weight / log2(1 + df)` where `df` is
  the number of notes carrying it and `kind_weight` is 1 for task and branch,
  0.5 for project, service and agent. An entity carried by more than
  `UA_MEMORY_GRAPH_ENTITY_MAX_DF` (25) notes is a hub and is not walked.

The leg expands recall; it does not re-rank. Seeds and every other lexical or
semantic hit are skipped before the fan-out cap, a note any content leg
matched (identifier matches included) gets no graph rank, and a graph-only
note takes the recency and entity rank it would slot into without shifting
any content match's rank (`memory_ranking.slotted_ranks`). So the fused
scores and order of content matches are exactly what they are with the leg
off, and a reached note slots in among them by the leg weight. (A first cut also
boosted content matches that were neighbours; on the M0 eval that lifted
filler notes sharing an entity with the top hit above the hit itself, cutting
lexical MRR from 0.837 to 0.792 and hybrid MRR from 0.927 to 0.910.)
**Bounded work.** Cost depends on the caps, not on how many links or notes
touch a seed. Per seed and direction, one statement reads at most
`10 x UA_MEMORY_GRAPH_FANOUT` link rows from the `(org_id, end, created_at)`
index (newest stored timestamp first; oldest first under `as_of`), applies
the exact historical cutoff, eligibility and the skip set **after** that raw cap in the same
statement, and keeps the first `UA_MEMORY_GRAPH_FANOUT` survivors. At most 16
of a seed's entities are walked (task and branch first). A hub check reads at
most `UA_MEMORY_GRAPH_ENTITY_MAX_DF + 1` rows of the
`(org_id, kind, value, note_id)` index, and a walk at most `_MAX_DF` rows.
`memory_get` scans at most 100 raw links per direction, oldest first, then
applies tenant and historical filters. A prefix of ineligible or newer links
can therefore yield fewer results; neither API scans beyond its budget to
fill those gaps.

Each seed keeps its `UA_MEMORY_GRAPH_FANOUT` (10) strongest eligible
neighbours. A neighbour's score sums `strength / seed_position` over the seeds
that reached it, and the leg keeps the best `UA_MEMORY_GRAPH_LIMIT` (20). The leg's rank is fused with weight
`UA_MEMORY_WEIGHT_GRAPH` (0.5); 0 skips the walk and reports
`retrieval.graph.status: "disabled"`.

**Validity.** A hop target must pass the search's own eligibility SQL (tenant,
not deleted, not an index note, tags, host, expiry, and not superseded, or
under `as_of` valid at that instant), before the fan-out cap, so an ineligible
neighbour never takes a slot. Under `as_of`, links recorded after it are not
walked.

**Tenant fence.** Each entity and link row carries its notes' `org_id`.
Writes insert through joins that require both notes in the caller's org, and
`BEFORE INSERT/UPDATE` triggers refuse any row whose `org_id` differs from its
notes'. Reads filter the edge row and both note rows on the caller's org, so a
cross-org edge never traverses even if one were written around the triggers.

`memory_get` returns `links: {out, in}` (`{id, rel, title, actor,
created_at}`, oldest first, at most 100 per direction).

**Default weight.** `UA_MEMORY_WEIGHT_GRAPH=0.5` puts the best graph-only
note at `0.5 / (k + 1)`, below a lexical-only match down to about rank 60, so
a reached note fills the tail of a short result list rather than displacing
a content match. Because the leg never re-ranks content matches, the M0 eval
(below) is unchanged at this weight; the fixture has no explicit links, so it
exercises only shared-entity hops, which add tail notes to 10 of
the 50 lexical-mode queries.

### Evolving confidence and note versions (task #2097)

Epic #1915 M7 and M8, after Hindsight's evolving opinion confidence and
Supermemory's version history, kept inside `memory.db`.

**Beta posterior (M7).** A note's four `memory_feedback` counters are the
evidence of a Beta posterior over "this note is right and useful", from a
uniform `Beta(1, 1)` prior. `memory_feedback` updates it by incrementing its
counter; nothing else is stored, so the posterior travels with the counters
through export and import and needs no backfill.

| Signal | alpha | beta | Why |
|---|---|---|---|
| `helpful` | +1 | | one success |
| `not_helpful` | | +1 | one failure |
| `stale` | | +0.5 | probably right once, has aged |
| `wrong` | | +2 | explicit falsification outweighs "did not help" |

`posterior = alpha / (alpha + beta)`.

**Contradictions.** An open `contradicts` link against a note lowers its
effective confidence at read time: each one adds 1 to beta (the weight of a
`not_helpful`), at most 5 counted, so
`effective = alpha / (alpha + beta + min(open, 5))`. Only the disputed note
(`dst` of `note -> disputed`) is lowered. A link is open while its disputing
note is live: in the caller's tenant, not soft-deleted, not expired and not
superseded. Under `as_of` it must also be valid at that instant (the search
rule) and the link recorded by then. The disputer clearing its `contradicts`,
being superseded, deleted or expiring closes it. Every one of these filters
runs in SQL before anything is counted, so closed links never crowd out open
ones; the count is then capped at 5. One grouped statement per search, so the
work grows with the `contradicts` links pointing at the candidate notes.
`memory_links` keeps no deletion history: under `as_of`, a link cleared after
that instant is not seen, so a contradiction that was open then but has since
been cleared does not count. Nothing is stored or overwritten: feedback
counters and the author-stated `confidence` field are untouched.

**Ranking.** The effective confidence is the confidence input of the
[prior](#search), `w_feedback * 2 * (effective - 0.5)`, inside the unchanged
[0.8, 1.2] clamp. A note with no feedback and no open contradiction is at 0.5
and gets a zero input, so it ranks exactly as before.

**Where it shows.** `memory_get`, `memory_feedback` and each `memory_search`
explanation carry `confidence_posterior: {alpha, beta, posterior,
open_contradictions, effective}`; search also reports the signed input as
`prior_inputs.confidence`. This is separate from `confidence`, the value an
author states with `memory_save(confidence=...)`, which still gates
`memory_promote` and packet injection and does not rank.

**Versions (M8).** `memory_versions(note_id, version, content, tags,
updated_at, actor, recorded_at)` holds each note's earlier states. A `BEFORE
UPDATE OF content, tags` trigger on `memory` (`memory_versions_capture`)
copies the row being replaced, with the `updated_at` and writer
(`updated_by`, else `author`) of that earlier state, whenever the content or
tags actually change. `recorded_at` is the capture time, the moment that
state was replaced, stamped by the trigger in the hub's
`YYYY-MM-DDTHH:MM:SS+00:00` format (second precision).
Being a trigger, it captures every path: `memory_save` replace and append,
the `memory_delete` tombstone and direct SQL. A write that
changes neither (the same text re-saved, salience, feedback, access counts,
supersede pointers) records nothing. Index notes are not versioned. Version
numbers rise per note and the newest 50 are kept. There is no backfill: a
note's history starts at its next edit.

`memory_get(id, include_versions=True)` adds `versions` (`{version, content,
tags, updated_at, actor, recorded_at}`, newest first, at most 20) and
`versions_total`. Reads join the note row and filter on the caller's tenant,
so another tenant's id stays "no memory note". With `as_of`, the history is
as it stood then: only versions with `recorded_at <= as_of`, the states
already replaced by that instant. A state that was current at `as_of` and was
replaced later is not listed, since its version did not exist yet;
`updated_at` (when a state was written) does not decide this.

**Deletion.** A soft delete keeps the versions, as its tombstone already
keeps the text. A hard delete (`storage.hard_delete_memory`, a migration
rollback, an index cleanup, or any `DELETE FROM memory`) erases them: the
foreign key cascades, and `memory_versions_on_memory_delete` also deletes
them on a connection with `foreign_keys` off. A reused id starts with no
history.

**Restoring a deleted note in place (task #2515).** `memory_restore` on a
soft-deleted note puts back the content and tags of its newest version that
is not a tombstone, under the same id. The note keeps its links (a delete
never removed them) and its version history; the tombstone itself becomes one
more version, so the deletion stays on record. Entities are rebuilt and the
tag indexes refreshed; the kept vector already embeds the restored text, and
chunk repair fills the chunks the delete dropped. The rule is delete's: the
owner, or a verified human, except that a note a verified human deleted
needs a verified human (the delete tags it `meta:deleted-by-human`; an older
tombstone is judged by its `updated_by`). A note deleted before version history existed has
no prior version to restore and is refused with that reason; its tombstone
still holds the text for a new note. `unified-agent-consolidate restore <id>`
uses this same path (task #2520): it undeletes a soft-deleted note or
re-promotes a superseded one, keeps owner, human-delete, tenant and
superseded-note return shape, flushes the vault snapshot before returning,
and prints "Restored … from deleted version …" versus "Restored … (was
superseded by …)" so the two cases stay distinct.

### Revisions, compare-and-set and browse reads (task #2515)

`memory_get` returns `revision`, `sha256:<hex>` over every column of the note
except the access and feedback counters, plus its outgoing links. A column
added later is covered without a change here. `memory_save` returns the new
one. Pass it back as
`memory_save(id=..., expected_revision=...)`: if the note changed in between,
nothing is written and the call fails with code `conflict`, carrying
`current_revision`. `updated_at` alone could not do this, because it has
one-second resolution: a replace read in the same second as a peer's append
would pass and drop the append, and a link-only change would not move it.
Feedback and access counts are not edits and leave the revision alone. The
check runs before the deleted-note refusal, so a stale save on a note deleted
since the read is a `conflict` with the tombstone's revision. Appends need no revision and work as before.

`memory_get(count_access=False)` reads a note without counting an access.
The Control Center's note view uses it, so a person browsing does not raise
`access_count` or `last_accessed`.

`memory_search(author=...)`, `memory_list(author=..., tags=[...])` and the
`tags` filter of both apply in the query, before any cap, so a filtered page
is the top of the filtered set rather than a filtered slice of an unfiltered
page.

### Auto-maintained index notes

Every save, delete and restore (and the consolidate apply and restore steps)
refreshes:

- One **master index** (fixed id `masterindex00`) cataloging every active tag, its note
  count, and its 5 most recent note titles.
- One **per-tag index** (id `tagindex_<sanitized-tag>_<6-hex-digest>` — the digest keeps
  tags distinct even if they sanitize to the same string, e.g. `project:acme` vs.
  `Project Acme`) listing every note carrying that tag, sorted newest-first, with a
  one-line extracted summary of each.

Indexes are built per tenant (task #1762): each catalogs only the caller's tenant's
notes and carries that tenant's `org_id`. The untenanted hub uses the ids above; an
enterprise tenant's index ids add a `-<12-hex digest of the org id>` suffix
(`masterindex00-…`, `tagindex_<tag>_<digest>-…`), so one tenant's titles, ids and
per-tag counts never reach another.

Index notes are tagged `meta:index` (tag indexes also carry `meta:index:tag`) and are
**excluded from `memory_search`/`memory_list`** unless a `meta:index*` tag is explicitly
requested — they're bookkeeping, not content, and would otherwise clutter every recall.
`memory_get("masterindex00")` is the fast path to "what tags exist and what's recent
under each." Tag indexes for tags that no longer have any notes are deleted
automatically on the next refresh.

The refresh runs after the note's own write commits, not inside it (task #1914):
the write queues its tags in `memory_index_dirty`, and a short separate
transaction rebuilds the queued indexes from one pass over the tenant's notes.
If that rebuild cannot run (busy past the timeout), the tags stay queued and the
next write, or a `memory_get` of an index note, rebuilds them.

### Passive session capture (task #813)

The retired `claude-mem` plugin's one capability UA memory lacked was summarizing a
session transcript at session end and storing it. `POST /api/session-capture` on the
hub (`{transcript_path, cwd, project?}`, bearer token, no CSRF) fills that gap with
zero provider spend while the local Ollama leg of `llm_client` answers:

- `session_capture.py` renders the transcript tail (last `UA_SESSION_CAPTURE_TAIL_CHARS`
  chars, default 12000), summarizes it through `llm_client.complete()` with a strict
  `{summary, facts[], decisions[], files[]}` schema, and stores the result via
  `memory_save` — tags `["session-capture", "project:<name>"]`, salience `0.4`,
  `expires_at` at `UA_SESSION_CAPTURE_RETENTION_DAYS` (default 45; session detail decays,
  durable facts get promoted by hand).
- **Dedup** rides `memory_save`'s idempotency key on the SHA-256 of the rendered tail
  (also embedded as a `session-sha256:` line in the note), so replaying the same
  SessionEnd hook is a no-op.
- The Claude Code side is `scripts/hook_session_capture.py`, a best-effort SessionEnd
  hook (any failure exits 0 — session end is never blocked). It reads the hub URL and
  bearer token from the existing `~/.claude.json` `unified-agent` MCP registration; no
  new secret. Wiring it into `~/.claude/settings.json` is a manual, reviewed step.
- If every provider leg is unreachable the endpoint returns `502` with the `route` —
  it fails loud, it does not store an empty note.

## Tasks

A durable, shared task queue (`tasks` table) — SQLite, no vector search, just structured
fields. Full signatures in
[tools-reference.md](tools-reference.md#tasks-toolstaskspy).

- **Titles:** `task_add` and `task_update(..., title=…)` refuse a missing or
  whitespace-only title (`title must not be empty`). A title that starts
  with `#` or contains a newline is also refused — pass a short summary,
  not the section/spec body.
- **Priorities:** `high` | `med` | `low`.
- **Statuses:** `open` → `doing` → `waiting_review` / `blocked` / `done` /
  `cancelled` (`tools/tasks.py` `STATUSES`). Setting `status="done"` stamps
  `completed_at`; any other status clears that stamp. Terminal statuses are
  `done` and `cancelled` — `task_claim` refuses both.
- **Subtasks:** `task_add(..., parent_id=<id>)` creates a child at any depth
  up to `TASK_MAX_DEPTH` (8). A parent may itself be a subtask. Reparenting
  a subtree that would cycle, or that would push a leaf past the cap, fails
  closed. Deleting a parent cascades through the whole subtree
  (`ON DELETE CASCADE`). Convention: the parent task stays `doing` until all
  of its subtasks reach `done`. Each `task_list` row includes `lineage`
  (ancestor ids, root-first). A `[project-spec]` root also reports `rollup`
  (derived status from the whole descendant subtree) and `project_view`
  (evidence count vs merge-verified PR count, plus the `#844` CLI pointer)
  and cannot be marked
  `done` while any descendant is still `open`/`doing`/`waiting_review`/`blocked`.
- **Claims:** `task_claim(id, agent=...)` is the atomic lock. It writes the
  typed `claimed_by` / `claimed_at` / `lock_expires_at` columns and appends a
  descriptive `claimed-by: <agent>` notes line. Ownership is the column, not
  the notes marker. A notes-only `claimed-by:` line never establishes a lock.
  A claim on a task someone else owns returns `safe_wait` and writes nothing
  ([tools reference](tools-reference.md)).
- **Attributed completion:** `task_update(..., status="done")` fails closed
  unless a `claimed_by` column is already set or the caller supplies a verified
  / explicit actor. On that transition the column is stamped. A notes-only
  `claimed-by:` line is not enough.
- **Keyed task transitions and worktrees:** when `task_update` receives an idempotency key, the logical task row, transition event, and idempotency receipt commit in one SQLite transaction. Worktree creation or removal remains a postcommit filesystem side effect. If that side effect is pending, the committed receipt returns `worktree="pending"`; retries replay that same snapshot and do not claim that the filesystem operation completed. Inspect the task row/worktree state separately; filesystem effects are not part of the database transaction.

### Scout task shape (task #1027)

FirstMate-style **scout** dispatches investigate and leave a standalone vault
report with a captain-facing **decision inventory** — they do not open a PR
branch. Stamp intake notes with `[task-shape: scout]` (or `[scout]`). The
Active Executor uses a scout prompt, skips the daemon commit/SHA evidence
path, and finishes with `evidence_ref=vault:<inbox-path>` plus `[no-code]` /
`[scout: no-pr]` / `[waiting-for: review]`. The review pipeline returns
`kind: scout`, validates the report's decision inventory, and notifies the
captain — it never merges. Template and contract:
[examples/scout_report.md](examples/scout_report.md).

### Queue lanes and WIP caps (task #705; actor model #883)

Every task has a `lane` column. `lane` answers **who acts on the task**, not
what it is about — the portfolio bucket lives on `domain` alone. Allowed values
are the system set plus every registered agent label
(`config.task_lane_values()`):

| Lane | Meaning |
|---|---|
| `default` | Untriaged shared pool; any agent may claim. |
| `dispatch-ready` | Triaged + vetted; review passes and the dispatch scheduler work from here. Claimable like `default`. |
| `legacy-operator-only` | A human step only the operator can do; never auto-dispatched. `task_claim` needs `expected_lane="legacy-operator-only"`. |
| `tracking` | An umbrella / roadmap index, not a unit of work — unclaimable. |
| `laptop` | Host-routed work for the laptop dev checkout. |
| *agent label* | Delegation-targeted work for that agent. |

The legacy lanes `personal` / `professional` / `hobby` are rejected on write
(pass them as `domain=` instead); the 2026-09 migration remaps existing rows to
`default`.

| How a lane is chosen | Result |
|---|---|
| `task_add(lane=…)` | That lane, if it is in the allowed set. |
| Omitted, subtask | Parent's lane. |
| Otherwise | `default`. |

`task_list(lane=…)` filters to one lane; omit it for the cross-lane view.
`task_claim(..., expected_lane=…)` is a guard, not a mutation:

- With `expected_lane` set, the task must be in `default` or that lane.
- With it omitted, only `default` or a lane matching the claiming `agent`
  label is claimable. A `laptop` task therefore needs `expected_lane="laptop"`.

WIP accounting counts `doing` rows whose lock has not expired. The caps bind
`task_claim` and, since task #1886, `task_update` moving a task into `doing`
(see "Entering `doing` through `task_update`" below):

| Cap | Env | On overflow |
|---|---|---|
| Per agent | `UA_AGENT_WIP_MAX` (default 20); `UA_AGENT_WIP_OVERRIDES` (`label:max`) sets one label's own cap | Raises `ToolError` |
| Per lane | `UA_LANE_WIP` (`lane:max:mode`, optional) | `hard` refuses; `soft` claims and returns `wip_warnings` |
| Fleet, soft | `UA_FLEET_WIP_MAX` (default 40) | Claims and returns `wip_warnings` |
| Fleet, hard | `UA_FLEET_WIP_HARD_MAX` (default 2 × soft, so 80) | Raises `ToolError`, except for urgent work |

Unlisted lanes have no lane cap. See [configuration.md](configuration.md).

The fleet cap (task #1838) puts a fleet-wide limit on the direct-claim path.
Before it, each agent could hold `UA_AGENT_WIP_MAX` tasks and
`UA_DISPATCH_MAX_LIVE_RUNS` counted only `dispatch_runs`, so direct claims by
several agents could reach six live runs with no signal. The soft cap defaults
to the same number as `UA_DISPATCH_MAX_LIVE_RUNS`, but the two are separate
gates over different ledgers: the fleet cap counts live claimed tasks, the
dispatcher ceiling counts `dispatch_runs` launch slots. They do not share or
combine a slot pool. The rules:

- **Count.** Claimed `doing` rows across every agent and lane whose lease has
  not expired, plus unarchived `doing` rows with no claimant that were written
  within the last hour (task #1886; see below). The task being claimed is not
  counted.
- **Soft, then hard.** A new claim with the count at or above the soft cap
  succeeds and returns a `wip_warnings` entry. With the count at or above the
  hard cap it is refused, so a brief overlap never blocks a claim but the
  fleet cannot pass the ceiling silently.
- **Urgent work is never refused.** A `high`-priority task, or one whose notes
  carry `[urgency: now]`, is claimed past the hard cap with a warning. Any
  agent can add the `[urgency: now]` marker, so the hard cap binds only
  `med`/`low` work without that marker unless the operator tightens it.
- **Exempt lanes.** `operator-only` and `interactive-only` claims are never
  warned or refused by the fleet cap. Their live claims still count.
- **Renewals.** A live lock renewal by the holder skips every WIP check, as
  before. A lane reassignment of a task already in `doing` adds no WIP and
  does not check the fleet cap.
- **Scope.** The cap applies to `task_claim` and to `task_update` entering
  `doing`. Dispatcher launches keep their own `UA_DISPATCH_MAX_LIVE_RUNS`
  gate.
- **Dispatched work.** A claim on a task that already holds a live dispatcher
  slot (a `dispatch_runs` row under the launch gate's live-slot predicate, or
  a claimed `node_dispatch` item with no receipt) is not warned or refused.
  The launch gate already admitted that run, so the spawned child's own
  `task_claim` cannot be refused after the approval was spent. The claim
  still counts toward the fleet total for every other claim.

#### Entering `doing` through `task_update` (task #1886)

`task_update(status="doing")` moves a task into `doing` without taking a claim
(`claimed_by` stays empty; only `task_claim` takes the lock). It used to skip
every WIP check, so it could fill the fleet past its caps invisibly. Now a
move into `doing` from any other status passes the same per-agent, lane and
fleet checks as a new `task_claim`, and returns the same `wip_warnings` or
raises the same `ToolError`:

- **Whose WIP.** The surviving claimant (a row leaving `done`/`cancelled` has
  its claim cleared, so it has none), else the verified caller. An operator
  dragging a task to `doing` in the web UI is a verified caller, so the move
  counts against the operator label's per-agent cap.
- **Lapsed claims.** A surviving claim whose lease has expired (most `blocked`
  tasks, after the default one-hour lease) would otherwise be counted by
  nothing once back in `doing`. If the caller is the claimant, the lease is
  renewed for the default `task_claim` length, as a re-claim would. Anyone
  else drops the dead claim, so the row counts as unclaimed WIP under the
  caller's run.
- **Same exemptions.** A row already in `doing` is not checked (the
  renewal-equivalent case). The exempt lanes, urgent work and a task holding a
  live dispatcher slot are treated exactly as on `task_claim`.
- **Parked tasks.** Admission reads the row the call leaves behind. A move
  into `doing` that leaves the row archived (already parked, or
  `archived=True` on the same call) is refused before any cap is consulted,
  as `task_claim` refuses a parked task. `archived=False` on the same call
  unparks it, and the row then faces the caps like any other.
- **Same claimability refusals** (task #1894). Before any cap, a move into
  `doing` is refused exactly where `task_claim` refuses: a task in the
  `tracking` lane (an umbrella, never a unit of work) and a task under a
  cancelled ancestor. Both read the row the call leaves behind, so a lane
  change or reparent on the same call is judged by the new lane and the new
  parent. The `expected_lane` scoping of `task_claim` does not apply: this
  path takes no claim.
- **Relaning.** Changing the lane of a task already in `doing` checks the
  destination lane's cap for every caller, fenced or not, and also the
  per-agent cap of the claimant (`UA_AGENT_WIP_MAX` or its override), else the verified
  caller: with that agent's other live WIP already at the cap, the relane is
  refused. A soft destination lane cap returns `wip_warnings`. The fleet cap
  is not checked, since the row adds no WIP. A move into `doing` with a lane
  change is checked once, against the new lane. Relaning a task in `doing`
  into the `tracking` lane is refused, as is re-asserting `status="doing"` on
  a row whose resulting lane is `tracking` (task #2002), so a `doing` row
  never lands in an unclaimable lane. A legacy `doing` row already in
  `tracking` can still be annotated, released, moved out of `doing` or
  relaned to a work lane.
- **Unverified and fenced calls are checked too.** A call with no verified
  identity gets the identity-independent lane and fleet caps, plus the
  per-agent cap of a live surviving claimant. Its `actor` is not a verified
  identity, so it is not charged a per-agent cap and its run is opened with no
  agent (`actor` is kept as `opened_by`). Two callers reach this path:
  - **Slack `/progress task-<id>`** (`slack_gateway.dispatch`) moves the task
    into `doing`. Its ingress, `/api/slack/events`, is a public signed-webhook
    path, so it carries no verified identity. At the fleet hard cap it is
    refused unless the task is `high`-priority or `[urgency: now]`; a live
    claimant's per-agent cap or a hard lane cap refuses it even for urgent
    work. The gateway replies in Slack that the update was not applied
    because of a WIP cap (the note is not recorded), instead of "queued for
    human review".
    On a blocked task whose claim has lapsed, it drops the holder's
    `claimed_by`, because the caller is not the holder. `/blocker` and
    `/review` move to other statuses and are not gated.
  - An MCP client on a hub without auth (stdio, or HTTP with auth disabled).

  The other in-process callers do not enter `doing` (the dispatch scheduler,
  bug worker and crew use other statuses; the executor re-asserts `doing`
  after its own `task_claim`). A call with `expected_run_id` is checked as
  well: leaving `doing` closes the run and clears the token, so a match on a
  row outside `doing` is an anomaly, not a continuation.
- **Actor-backed start without hub auth.** Because an unverified start's run
  has no agent, a follow-up that names the same `actor`, such as
  `task_update(t, expected_run_id=<run>, worktree=True, actor=X)`, is refused
  with `worktree provisioning run_not_current`; the same call without `actor`
  passes. This only affects deployments without hub auth, where `actor` is
  the only attribution.
- **How an unclaimed row counts.** It has no lease, so it is treated as if it
  held a default-length `task_claim` lease (one hour,
  `UNCLAIMED_DOING_LEASE_SECONDS`) from its last write: any update to the row
  renews it, and an hour without one frees its slot, like an expired lease.
  Archived rows never count. `queue_pressure` still reports every unclaimed
  `doing` row as `unowned_doing`, whatever its age.
- **Per-agent attribution.** An unclaimed row counts toward the per-agent cap
  of the agent its open self-pickup run is attributed to
  (`tasks.current_run_agent`, written in the same transaction as the move).
  A row with no recorded run agent (a move with no verified identity) is
  attributed to nobody and binds only the lane and fleet caps. The lane count
  uses the same one-hour rule for unclaimed rows (it used to count them for as
  long as they stayed in `doing`).

`observability_manage(action="runs_in_flight")` reports both gates under
`wip`: `fleet` (live claims, `unclaimed_doing` (the part of them with no
claimant), soft and hard caps, `state` of `ok`,
`over_soft_cap` or `at_hard_cap`, exempt lanes), `dispatch` (live dispatch
slots and their cap) and `running_by_path`. Each item carries `claim_path`:
`dispatcher`, `executor`, `direct_claim`, `task_update` or `unrecorded`.

### Queue hygiene (task #729)

`unified-agent-review scan` runs `queue_hygiene.run_hygiene_pass` before
reconcile. The pass is marker-gated (`[hygiene: <kind> @ ts]` in notes) and
does not call a model:

1. **stale-claim** — `doing` with expired `lock_expires_at` is a candidate.
   `lease_reaper` probes the owner (`owner_liveness`). A terminal owner (its
   run is recorded as over) or an absent one (no agent row, no node) returns
   the task to `open` with the claim columns cleared. When the owner's
   self-pickup run is still open with no recorded end, the work is
   interrupted instead: the task goes to `blocked` with `waiting_on` and an
   `[interrupted: run=…]` marker, and one `run.interrupted` event lands on
   `agent.run.stalled`; resuming it is an explicit claim. That `blocked`,
   unowned state is a hold (#2482): while the interrupted run is still the
   task's newest, `delegate_claim`, approved dispatch, `dispatch_resume`, node
   dispatch and a `task_claim` that rides a queued delegation are all refused,
   and `delegate_list` marks the pending row `held`. A claim by an agent with
   no delegation queued for the task resumes it; reopening it lets queued work
   run; closing it ends it. A lease whose own run is recorded as ended is
   never renewed first (#2483), so the reaper sees it in the same pass. With
   no live agent
   or node, an owner whose every hub session is recorded ended after the
   claim also counts as terminal. An active or unknown
   owner keeps the claim; unknown ones are listed under `leases_kept` and make
   the doctor's `stale_task_locks` check DEGRADED. The age of a lease alone
   never releases a claim, and a done or cancelled task keeps who closed it.
2. **aging-demote** — `open` older than 14 days drops one priority step
   (`high`→`med`→`low`; `low` stays). Once.
3. **auto-archive** — `done` completed more than 30 days ago gets
   `archived_at`. Once.

A hygiene failure is a stderr warning. It never blocks the review pass.
Details: [automation.md](automation.md#queue-hygiene) and
[web-ui.md](web-ui.md#flow-metrics-task-729).

`task_list(status="all")` is the way to see every lifecycle status at once; the default
also excludes explicitly archived tasks. Pass `archived=True` to list archived rows.
`status="open"` only shows the open queue.

- **Project streams:** every task carries a `project` slug — `unified-agent` (the
  default, and what every pre-#327 task backfilled to) | `browser` | `sample-store` |
  `client-portal`. `task_list(project=<slug>)` scopes to one stream; omitted returns every
  project — the cross-project view. `task_update`/`task_claim` take an
  `expected_project` guard (reject the call if the task isn't in that project) so a
  project-scoped session can't accidentally mutate another project's task by id.
  Full parameters in [tools-reference.md](tools-reference.md#tasks-toolstaskspy).

### Compact listings (`fields="compact"`)

`task_list(fields="compact")` drops the `notes` body from every row and returns
`notes_chars` plus the typed `claimed_by` column (not a parse of the notes
line). It also projects queue-marker booleans (`waiting_for_review`,
`laptop_lane`, `notes_archived`). The default `fields="full"` is unchanged.

`waiting_for_review` is true only when a **live** `[waiting-for: review]`
marker survives after stripping the forward-looking suffixes ` when done` and
` once shipped` (`tools/tasks.py:_FORWARD_LOOKING_REVIEW_QUALIFIERS`) **and**
`status` is not `done` or `cancelled`. The marker is appended once and never
retracted, so a shipped task would otherwise stay `waiting_for_review=true`
forever (compact projection fix, 2026-09-16). A later bare live marker still
counts even if an earlier line said `once shipped`.

The review pipeline (`reviewer.py`) is stricter: scan/reconcile skip only
`[waiting-for: review] when done`. `[waiting-for: review] once shipped` still
enters the review queue. Use `when done` on unclaimed spec templates.

Notes dominate payload size on long-lived queues, so use compact for routine queue
syncs and status sweeps — then fetch full notes on demand with a narrowed
`fields="full"` call (e.g. `notes_contain` or `parent_id` filters) only when you
actually need a body. `sync.py`'s snapshot generator follows the same rule: it scans
notes for matching and claiming but never carries bodies into its output.

## Vault (Obsidian bridge)

`UA_VAULT_PATH` (default `~/vaults/main`, PARA layout) is the human-readable knowledge
base. Agents get **read-anywhere, write-only-to-allowlisted-subdirs** access — the
boundary is enforced in `safety.resolve_in_vault()` (see
[security.md](security.md#vault-sandbox)), not by convention. Setting
`UA_VAULT_PATH=""` disables every `vault_*` tool.

### Three layers

1. **`00 - Inbox/`** (raw captures) — agents drop drafts and summaries here for the operator
   to file into curated PARA folders. Never rewrite another agent's or the user's
   existing capture in this layer.
2. **`Wiki/`** (agent-maintained) — the LLM-wiki pattern: synthesize inbox captures into
   cross-linked `Wiki/` pages, and keep `Wiki/Index.md` current as new pages are added.
   Prefer updating an existing wiki page over creating a near-duplicate.
3. **Curated PARA folders** (`01 - Projects`, `02 - Areas`, etc.) — human-only. Agents
   have read access but propose changes as tasks or inbox notes rather than editing
   directly.

`memory_*` (SQLite) stays the authoritative agent memory with semantic search; the vault
is the *human* surface — division of labor, not duplication.

### Concurrent edits: revisions (task #2111)

Two agents can read the same note, edit it and write it back. Without a check the
second write silently replaces the first. Every `vault_read` returns a `revision`,
`sha256:<hex>` of the note's raw bytes on disk (not of expanded embeds or the page
window), and every successful `vault_write` returns the note's new one. Pass the
revision you read back as `vault_write(..., expected_revision=<revision>)`:

- if the note is unchanged, the write lands and returns the new revision;
- if anyone changed it in the meantime, the write is refused with the current
  revision in the error and nothing is written. Re-read, merge your change into the
  current content, and write again with the new revision;
- `expected_revision="absent"` creates a note only if it does not exist yet.

The check runs inside the per-note lock that the `locked: true` guard uses, after
that guard, so a locked note still reports as locked, and of two writers holding the
same revision exactly one succeeds. It covers overwrite, append and template writes.
Omitting `expected_revision` keeps the old unconditional write. A hub that sets
`UA_VAULT_REQUIRE_REVISION=1` ([configuration.md](configuration.md)) refuses an
overwrite of an existing note without one; creates and appends stay allowed.

The hub's own read-modify-write writers go through `vault_write_merged` in
`tools/vault.py`: read the note and its revision, rebuild the content, write with that
revision. Merge-aware callbacks re-read, rebuild and retry up to three more times
(`MERGE_WRITE_RETRIES`). Whole-page replacements (learning pages, decision packets,
goal proposals) set `retries=0` and surface a concurrent edit as a conflict; they
cannot adopt a newer revision for an unchanged stale payload. Snapshot text uses
universal newlines while revisions continue to hash the original raw bytes.
`memory_promote` (the learning page and the `Wiki/Index.md` block), the planning loop
(the plan page, the dated decision packet and goal proposals) use it. The delegation
ledger creates its month page create-only (`expected_revision="absent"`) and appends if
another writer created it first. All of them keep working with
`UA_VAULT_REQUIRE_REVISION=1`.

`vault_read` also returns `locked` (task #2515): the verdict `vault_write`'s
guard reaches on the same bytes, true for frontmatter `locked: true` and true
when the frontmatter is too long to decide (the guard fails closed). A reader
can show a note as locked without parsing its frontmatter itself.

### Session learning proposals (validate-and-hold)

Completed sessions may emit a reviewable proposal via `propose_session_learnings` (also
called best-effort from `session_end(status="completed")`). Extraction is
deterministic and local: it reads the session summary plus sanitized lifecycle
`tool_end` error→success pairs. It does not call a model, read a transcript, or
write `memory_save`. Each candidate carries `confidence`, `provenance`,
`epistemic: inferred`, `evidence` (`session:`, `task:` and, for lifecycle pairs,
the two `event:` ulids) and `observed_at`. Duplicates of promoted memories and
earlier Inbox proposals are dropped, except that a near-duplicate live note
that disagrees with the candidate (key/value or antonym conflict) is listed as
`contradicts` with its owner instead (same-tenant notes only): the reviewer supersedes it if it is their
own note, or links it with `contradicts` and leaves it to its owner. Nothing is
overwritten automatically.
Empty or low-signal sessions create nothing. Repeated `session_end` with the
same candidates is idempotent; changed evidence refreshes the same file.

`session_end` may also return `friction_nudge` (task #914). That is a
**nudge only**: it never calls `memory_save`, never writes an Inbox note, and
never opens an approval. It scores interrupts, denied tool calls, re-prompted
corrections, and retried tool failures (default threshold 20 — one primary
signal crosses it; tool-diversity / skill bonuses cannot). Sessions with
fewer than `UA_FRICTION_TOOL_GATE` tool calls (default 15) never nudge.
One nudge per session (`session_friction_nudges`). Disable with
`UA_FRICTION_NUDGE=0`. The follow-up is still `propose_session_learnings`.

The file lands in `00 - Inbox/Session-Learning-<session_id>.md` with
`applied: false` and `authority: human-review`. Event and summary text is
untrusted historical data, not instructions. Secret-shaped material is
redacted or refused. A human reviews the note, then optionally runs an
attributed `memory_save`. The sleep-pass contract is unchanged.

### Skill-cluster proposals (validate-and-hold)

High-trust promoted memories may emit a reviewable skill outline via
`propose_skill_clusters` (also called from the nightly sleep pass before any
LLM work, and from `unified-agent-evolve`). There is no separate `fact_store`:
the pass reads `memory` rows tagged `fact` or `decision`, drops expired /
superseded / session-capture / agent-identity notes, and refuses
injection-shaped or secret-only text. Clusters stay inside one
project + author + source_host. Recurring evidence is required (minimum two
notes). The artifact is `00 - Inbox/Skill-Cluster-Proposals.md` with
`applied: false` and `authority: human-review`.

This is **propose-only**. Skills and rules are red-tier: the pass never
writes `SKILL.md`, never installs a host skill or rule, never calls
`memory_save`, and never auto-approves. Source excerpts are untrusted
historical data, not instructions. A human reviews the Inbox note, then
optionally authors a skill by hand and runs `skill_review`.

### Duplicate-cluster proposals (sleep pass / consolidate)

Near-duplicate clustering (`consolidate.cluster_duplicates`) is greedy
connected-components over embedding cosine similarity. The pairwise cosine
pass is one BLAS matmul (`_pairwise_cosine_matrix`, task #997), not a Python
double loop, which is why the sleep-pass admission ceilings can sit at 6,000
candidates / 18M pairs. Clustering still only **proposes**; apply is
`unified-agent-consolidate` / the explicit supersede step. That step is
tenant-fenced, refuses to re-point a note already superseded by another
survivor (restore it first), logs the actor to `agent_events` (the demoted
note's `updated_by` stays its author's), and refreshes the index notes
(task #1914). It also stamps the demoted note's `valid_to` (task #2095). Expired notes
still count toward admission — consolidate does not filter `expires_at`.
Ceilings and deferral: [sleep-pass CLI](cli-reference.md#unified-agent-sleep-pass).


### `vault_journal`: bookkeeping

The designated chokepoint for recording notable actions (commits, decisions, task
completions): `vault_journal(entry, agent=<label>, tags=[...])` appends a line to
`Logs/YYYY-MM-DD.md`:

```
- **HH:MM** [Agent_Label] entry text #tag1 #tag2
```

Creates the daily note (with a `date`/`type: journal` frontmatter block and an H1) on
its first write of the day. Use your agent label from shared memory conventions
(`agent-claude`, `agent-agy`, `agent-codex`, `agent-hermes`,
`agent-laptop-agent1`, `agent-laptop-agent3`, `Laptop_Agent5`).

A scheduled job that would write a line per event keeps those lines in its own
per-day note, `Logs/<job>/YYYY-MM-DD.md` (same line format and entry cap,
`journal_stream_append` in `tools/vault.py`), and puts at most one pointer line a
day in the shared journal. pr-watch is the first such job
(`Logs/pr-watch/YYYY-MM-DD.md`, task #1920). `export_data(action="journal")`
reports those lines with the job name as `machine`.

### Wikilinks and the graph

`[[target]]`, `[[target|alias]]`, and `[[target#heading]]` are all recognized (the
shared regex, `WIKILINK_RE` in `tools/vault.py`, strips the anchor/alias, leaving the
bare target). `vault_links(path)` returns a note's outgoing links plus its backlinks —
any other note whose `[[target]]` matches this note's filename stem or vault-relative
path (minus extension). Use this for graph navigation instead of re-grepping the vault
by hand.

### Wiki custodian (`Wiki/Health.md`)

`vault_wiki_custodian` (tool) / `unified-agent-wiki` (CLI) — see
[cli-reference.md](cli-reference.md#unified-agent-wiki) — audits `Wiki/` and rebuilds
`Wiki/Health.md` wholesale on a live run. `dry_run=True` / `--dry-run`
reports the same payload and writes **nothing**: it does not create
`Wiki/` or `00 - Inbox`, and it does not rewrite `Health.md`
(`index_rebuilt` stays `false`). On a hosted hub the empty-vault guard
(`refuse_unpopulated_vault`) still runs first, so a missing or empty vault
is refused before the walk. Live run:

- **Tag directory**: every `Wiki/` note grouped by frontmatter/inline `#tag`, with
  untagged notes grouped under `untagged`.
- **Recent updates**: the 10 most recently modified notes.
- **Pending inbox capture tasks**: files sitting in `00 - Inbox/` awaiting synthesis.
- **Broken links**: a `[[target]]` in a `Wiki/` note that doesn't resolve to another
  `Wiki/` note, `Index`, or any file elsewhere in the vault (single vault-wide filename
  scan, done once per run rather than once per broken link). Asset embeds
  (`![[diagram.svg]]`) resolve against real filenames, not `.md` stems only.
- **Orphan pages**: `Wiki/` notes with zero incoming links from other `Wiki/` notes.
- **Duplicate candidates / missing aliases / empty stubs / stale pages**:
  identity collisions, pages without an alias, short stubs, and notes older
  than `UA_WIKI_STALE_DAYS` (default 90).
- **Ambiguous names**: one stem, several paths, and a *human-written* bare
  `[[Name]]` somewhere in the vault. Folder-qualified links are fine.
- **Index coverage**: pages that exist but are not linked from `Wiki/Index.md`
  (`unindexed_pages`). Backlinks from elsewhere keep them off the orphan list,
  so this is the only check that sees them.

Link extraction skips `Wiki/Health.md` (this report) and the auto-generated
`Logs/state_memory*` / `Logs/state_tasks*` mirrors. Those files echo wiki
names and would otherwise re-report the same non-findings every run (UA #855 /
task #206). A hand-written journal `[[Shared]]` still counts.

`Wiki/Health.md` is deliberately separate from `Wiki/Index.md` — the custodian rebuilds
`Health.md` wholesale every run (excluded from its own analysis, along with `Index.md`
itself), but never touches the curated `Index.md` map, which is agent-maintained by
convention (the steward-inbox skill / wiki-synthesis workflow), not machine-generated.

## Offline state sync (`sync.py`)

For the operator-controlled Mac snapshot fallback and its ownership boundary, see
Vault sync operations. That procedure is staging-only;
it does not replace Obsidian Sync or establish a second automatic transport.

On every memory/task mutation (and once at server startup, in a background thread so it
never blocks MCP handshake), `sync_state_to_vault()` writes four files into the vault's
`Logs/` directory, atomically (temp file + `os.replace`, so concurrent readers never see
a torn write):

- **`Logs/state_memory.<host>.txt`** / **`Logs/state_tasks.<host>.txt`** — full JSON dumps of the
  `memory` and `tasks` tables (index notes excluded from the memory dump).
- **`Logs/state_memory.<host>.md`** — a human-readable Markdown snapshot: one section per note
  (title, tags, author/updated-by, timestamps, content — backticks escaped to prevent
  breaking the surrounding code fence).
- **`Logs/state_tasks.<host>.md`** — a Markdown snapshot with a summary directory table (id,
  title, status, priority, assignee parsed from `claimed-by:` in notes, updated-at) plus
  a detailed queue grouped by status.

PR events in `Logs/pr-watch/<day>.md` remain in briefing entries (`machine: pr-watch`,
`pr_watch_count`), activity-index journal events, and the nightly sleep-pass digest.
Briefings bound the stream to 64 KiB; sleep-pass bounds the combined daily and stream
input to its journal limit. Concurrent polls check and append the shared daily pointer
under one file lock, so only one pointer and its activity event are recorded.

The two `.md` snapshots are capped at 1.5 MB so Obsidian can render them; a
capped file says so right after its frontmatter and names the `.txt` mirror,
which is never capped and is the only form code reads
([troubleshooting](troubleshooting.md#vault-state_md-files-look-truncated)).

This exists so any host without a working MCP connection to the shared database can
still read the current memory/task state directly from vault files. Controlled by
`UA_STATE_SYNC` (default `1`; set to `0` to disable). A sync failure (e.g. vault
unreachable) is logged and swallowed — it never fails the underlying memory/task
mutation that triggered it.

The `.md` mirrors are capped at **1.5 MB** (`sync.MD_MIRROR_CAP_BYTES`). Obsidian's
renderer OOMs on markdown past roughly 2 MB. Leading entries are kept; a truncated
file has an explicit notice after frontmatter and at its end naming the matching
full `.txt` JSON mirror. The `.txt` files stay full — they are the
machine-readable fallback, not a backup. Use
[`unified-agent db backup`](database-operations.md) for a consistent snapshot.
