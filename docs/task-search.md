# Ranked task search (task #1419, Parity 14)

Gap this closes: `memory_fts` was the only fts5 table in `storage.py`. Task
lookup was substring-only (`task_list(notes_contain=...)`) — no ranking, no
relevance rubric. This adds a second fts5 table, `task_fts`, and a ranked mode
on the existing `task_list` tool. It does not add a new object kind, a new
task store, or a second `task_list`-shaped tool.

## Where it lives

| Piece | File |
|---|---|
| Schema (task_fts, sync triggers, backfill, task_embeddings), ranking | `src/unified_agent/task_search.py` |
| `ensure_schema` hookup (one call, next to `goals.ensure_schema`) | `src/unified_agent/storage.py` |
| `task_list(query=...)` ranked-mode branch | `src/unified_agent/tools/tasks.py` |
| Control Center query box endpoint | `GET /api/tasks/search` — `src/unified_agent/webui/api.py` (`task_search_payload`), `src/unified_agent/webui/routes.py` (`_task_search`) |
| Tests | `tests/test_task_search.py` (unit/contract), `tests/test_task_search_eval.py` (MRR/recall harness), `tests/test_webui_task_search.py` (HTTP route) |

## API shape

`task_list` gained one new parameter, `query: str | None = None`, rather than
a second tool or a forked signature:

```
task_list(status="all", query="login race condition")
# -> {tasks: [...], truncated: True|False, next_cursor: None, limit: 20, ranked: True}
```

Rules:

- **Mutually exclusive with `notes_contain`.** Passing both raises `ToolError`
  — ranked and substring search are two modes on one tool, not a merged
  query language. The substring path's SQL (`notes LIKE '%...%'`) and
  behavior are byte-for-byte unchanged when `query` is absent.
- **Every other filter still applies.** `status`, `project`, `domain`,
  `lane`, `priority`, `source_host`, `archived`, `parent_id`, `office`/`role`,
  `due_before`, `completed_after`, `frontier`, `stale_claims`,
  `created_since`/`updated_since`, `local_day`/`tz` all compose with `query`
  exactly as they compose with the default listing — `task_list` builds one
  `where`/`params` pair from all of them and ranked mode consumes the same
  pair the substring path would have, so the two can never silently diverge
  on what counts as a match.
- **No cursor pagination in ranked mode.** `cursor` is rejected (`ToolError`)
  when `query` is set. Ranked mode returns a fixed top-`limit` list
  (`limit` capped at 100) with `truncated` reporting whether more candidates
  were ranked, and `next_cursor: None` —
  a relevance-ordered top-K is a different pagination shape than a
  priority/age keyset walk, and mixing them would make "page 2" mean
  "next 20 by relevance from an unstable ordering" the moment any task's
  score changes between calls.
  Increase `limit` (up to 100) to widen the top-K. `truncated: True` at 100
  means the fixed cap hides further candidates; the API does not paginate them.
  The FTS retrieval cap is 200 eligible hits; search also reports
  `truncated: True` when that cap has more lexical matches behind it.
- Each returned task carries `combined_score` (float) and `explanation`
  (dict) in addition to every field `task_list` normally returns.

## The combined-score formula

The documented memory-search formula (see this repo's own `AGENTS.md`,
"Memory Search & Ranking") is:

```
combined_score = w_semantic · S_semantic + w_fts · S_fts + w_recency · R_recency
                + w_salience · S_salience + w_access · A_access
```

This task reuses that formula rather than inventing a second ranking — but
two of its five terms have no equivalent on a task row:

| Term | Applies to tasks? | Why |
|---|---|---|
| `S_semantic` | Yes | Cosine similarity between the query embedding and a `task_embeddings` row (title + notes), same fastembed model memory uses. |
| `S_fts` | Yes | Normalized `bm25(task_fts)`, identical normalization memory_search uses: `min(10, max(0, -bm25)) / 10`. |
| `R_recency` | Yes | `2^(-age_days / half_life)` against `tasks.updated_at` — a task's own recency, not a memory note's. |
| `S_salience` | **No** | `memory.salience` is a memory-note column with no task equivalent. `tasks.priority` is a *workflow* field (queue ordering: high/med/low), not a relevance judgment about how well a task matches a query — folding it in would be inventing a signal under the documented formula's name, not reusing one. Fixed at `0.0`. |
| `A_access` | **No** | `memory.access_count` is bumped by `memory_search` itself on every hit; there is no equivalent read-counter on `tasks`. Fixed at `0.0`. |

So for a task, `combined_score = w_sem·S_semantic + w_fts·S_fts + w_rec·R_recency`.
`explanation.salience` and `explanation.access` are `None` (not `0.0`, not
omitted) and `explanation.not_applicable` names both terms and why, so a
caller reading the breakdown never mistakes "not applicable" for "scored
zero." This intentionally does **not** reuse memory_search's newer
entity/feedback/vote terms — those are extensions memory_search has grown on
top of the documented formula (task-id/branch/agent identifier boosts,
user feedback signals, recall-curation votes), not part of the formula this
task was asked to reuse.

### Weights (env-configurable, same pattern as `UA_MEMORY_WEIGHT_*`)

| Variable | Default |
|---|---|
| `UA_TASK_SEARCH_WEIGHT_SEMANTIC` | `1.0` |
| `UA_TASK_SEARCH_WEIGHT_FTS` | `0.5` |
| `UA_TASK_SEARCH_WEIGHT_RECENCY` | `0.3` |
| `UA_TASK_SEARCH_RECENCY_HALF_LIFE_DAYS` | `15.0` |

A malformed or negative value falls back to the default (never inverts or
disables a signal) — same guard `_get_weight` uses in `tools/memory.py`.

### `explanation` shape

```json
{
  "why": ["lexical", "semantic"],
  "lexical": 0.031,
  "semantic": 0.71,
  "recency": 0.94,
  "salience": null,
  "access": null,
  "not_applicable": [
    "salience (memory-only column; tasks have no relevance-importance field -- priority is a queue-order field, not reused here to avoid faking a signal)",
    "access (memory-only column; tasks have no read-access counter)"
  ],
  "snippet": "Duplicate sessions appear when two [login] requests [race]."
}
```

`why` lists which signals actually fired for this result (`"lexical"` if it
was an FTS hit, `"semantic"` if similarity >= 0.55, `"recency"` as a fallback
when neither fired but the row was still a semantic-widening candidate).

## Schema: task_fts and task_embeddings

`task_fts` is an **external-content** fts5 table — `content='tasks'`,
`content_rowid='id'` — the exact rowid-mirroring pattern `memory_fts` already
uses (`content='memory'`, `content_rowid='rowid'`; see the `memory_vec`
comment block in `storage.py`). It indexes `title` and `notes`, kept in sync
by `AFTER INSERT/UPDATE OF title, notes/DELETE` triggers on `tasks` — no
column was added to `tasks` itself, so this cannot collide with the
concurrent `#1353`/`#1425` branches touching that table.

`task_embeddings(task_id, embedding)` mirrors `memory_embeddings(note_id,
embedding)`: one row per task, populated best-effort (a task must stay
searchable via FTS even when fastembed can't load — same swallow-and-degrade
contract `_store_embedding` documents in `tools/memory.py`). Similarity is
computed with the same `cosine_similarity` SQL UDF `storage.connect()`
already registers on every connection for `memory_embeddings` — no new UDF,
no sqlite-vec acceleration table (task-scale doesn't need it; if it ever
does, the same `memory_vec` mirror pattern applies unchanged).

### Backfill

`task_search.ensure_schema(conn)` is called once, right after
`goals.ensure_schema(conn)`/`task_comments.ensure_schema(conn)` in
`storage._migrate`, on every process startup. It is idempotent: it creates
the table/triggers (`CREATE ... IF NOT EXISTS`, a no-op past the first run)
and then backfills FTS rows for any task whose id is missing from
`task_fts_docsize` — fts5's own shadow table tracking true index membership.

That last detail matters and is worth a full sentence: a plain
`SELECT ... FROM task_fts` with **no** `MATCH` clause on an external-content
fts5 table reads straight through to `tasks` by rowid rather than reporting
the FTS index's own state, so it cannot be used as the "is this row indexed"
anti-join — it would report every task as already indexed even the instant
after its FTS entry was deliberately removed. `task_fts_docsize` is what
reads consistently as index membership; `tests/test_task_search.py::
test_task_fts_backfills_rows_created_before_the_table_existed` pins this
directly (delete the docsize row, not a plain `DELETE FROM task_fts`, which
is fts5's own required way to remove an external-content row, and re-run
`ensure_schema`).

Embedding backfill starts in a daemon thread from `storage.init_db`. It scans
missing tasks in 32-row batches, commits each batch, and stops on model failure
so FTS remains usable. A later startup resumes from the missing rows. In
addition:

- `ranked_task_search` embeds up to 32 eligible uncached tasks per query,
  starting with the most recently updated tasks independently of FTS and
  then lexical candidates. This lets a synonym-only query find recent tasks
  on a cold cache. Older uncached tasks can remain absent from semantic-only
  results while startup backfill runs; lexical retrieval remains available.
- `task_search.backfill_task_embeddings(limit=200)` exists for an explicit,
  wider warm-up (a maintenance task, a one-off CLI run) ahead of query time,
  should that ever be wanted.

## Evaluation harness

`tests/test_task_search_eval.py` is the reproducible harness the DoD asks
for: a fixed 22-task fixture corpus across five topics (auth, deploy,
billing, docs, testing) plus five noise tasks, one query per topic with its
known-relevant task ids, and two assertions:

- **MRR and recall@3** across all five queries, thresholded well below what
  the corpus measures at authoring time (MRR 1.0, mean recall@3 0.833) so the
  test catches a real ranking regression without being sensitive to bm25's
  exact behavior on a small, fixed corpus.
- **No noise task ever ranks first** for a topic query — a sanity check
  independent of the numeric thresholds.

Run it standalone for a score readout:

```
cd .worktrees/wt-1419 && PYTHONPATH=src .venv/bin/pytest -q tests/test_task_search_eval.py -s
```

Queries in the fixture are single literal tokens known (by inspection of the
corpus) to recur across each topic's tasks — chosen to exercise topic
separation without also depending on bm25's exact behavior across a mix of
common and rare terms. Neither fts5 table here has a stemmer configured, so
`"invoice"` and `"invoices"` are still different tokens either way.

### FTS query semantics: OR, not AND (differs from memory_search)

`task_fts`'s `MATCH` query **OR-joins** tokens — each token is quoted as its
own fts5 phrase and the phrases are joined with `OR`
(`_sanitize_fts_or_query` in `task_search.py`). This is a deliberate
divergence from `memory_search`'s `fts_query`, which joins quoted tokens with
an implicit AND (bareword/phrase terms separated by whitespace default to
AND in fts5). `memory_search` itself is unmodified by this task.

The reason: a *ranked* search over natural language should let bm25 order
partial matches, not return nothing because one word out of several didn't
land in the same row. A query like "who is working on the login bug" against
AND semantics needs `who`, `is`, `working`, `on`, `the`, `login`, and `bug`
all present in one task — vanishingly unlikely — whereas OR semantics lets
the task containing "login" surface (and rank above a task matching only
"working"), which is what a human typing a sentence into a search box
expects. `tests/test_task_search.py::
test_natural_language_query_finds_via_fts_alone` pins this with semantic
scoring explicitly disabled, so the assertion is about FTS alone.

**Injection safety**: quoting each token as its own phrase neutralizes fts5's
special operators (`AND`/`OR`/`NOT`/`NEAR`, `^column`, `*prefix`, `-exclude`,
parentheses) when a caller's query happens to contain them literally — those
only carry syntactic meaning *outside* a quoted phrase. The one character
that still escapes a quoted phrase is an embedded double-quote (it closes the
phrase early), so `_sanitize_fts_or_query` strips `"` from each token before
quoting it. A token that becomes empty after stripping is dropped rather than
emitted as `""`, which fts5 rejects as a syntax error.

## Control Center query box

`GET /api/tasks/search?q=<query>&project=<optional>&limit=<optional, default 20>`
is a thin, `_safe`-wrapped pass-through to `task_list(query=..., status="all")`
— all ranking logic stays in `task_search.py`/`tools/tasks.py`; the route
adds nothing but the HTTP contract every other Control Center read route already
has (origin/auth guard via `_read_only`, `{error}` shape on failure, task
links via `_task_object_links`). It is registered in
`src/unified_agent/webui/routes.py`, not `server.py` — no MCP surface change
was needed for the HTTP side. `notes_contain` substring search is
deliberately **not** exposed over HTTP; it stays MCP-only, since ranked mode
is what a human typing into a search box wants.

## What was deliberately not done

- No new object kind, no new task store — `task` was already declared;
  ranking is read-only over the existing `tasks` table plus two new,
  self-contained tables (`task_fts`, `task_embeddings`).
- No change to `notes_contain`'s SQL, return shape, or semantics.
- No `SCHEMA_VERSION` bump — `task_search.ensure_schema` follows the
  module-owned idempotent `ensure_schema(conn)` pattern `goals.py` already
  established, so three parallel branches touching `storage.py` this week
  don't collide on that constant.
- No sqlite-vec acceleration table for `task_embeddings` — task-scale doesn't
  need it yet; if it does, `memory_vec`'s mirror pattern is the template.
