# Memory evaluation baseline — task #1458

**Status:** DEFER Hindsight and Supermemory adoption. The native baseline below
passes a small frozen synthetic fixture, but neither candidate has been run
under the task's no-install, no-credential, single-authority boundary. This is
not evidence that native memory is better; it is insufficient evidence to
replace or duplicate the current authority. Keep the native path in service.

## Current contract and seams

The hub's `memory.db` and `memory_save`/`memory_search` own persisted notes,
tags, source host, supersession, and explainable retrieval. gbrain, Browser,
codebase-memory, and the vault remain separate knowledge sources; this task's
target is one scoped query, identity, and audit contract across them, not a
second authoritative memory store. The existing relevance-injection (#836),
experience-extraction (#837), and skill-cluster (#839) seams should be
evaluated rather than rebuilt. The planned single MCP gateway (#1336) is the
integration boundary for those sources. This fixture exercises only the
native `memory_search` path and does not test that gateway or those seams.

## Frozen native fixture

`tests/fixtures/memory_eval_1458/cases.json` is the active versioned v2
acceptance fixture; `cases_v1.json` preserves the initial version. V2 repairs
the two supersession queries so both old and current notes match lexical
terms. It contains eight synthetic notes and six fixed queries. The content,
identifiers, dates, project names, broadcasts, and checksum are invented test
data; it contains no user, customer, or production memory. The cases cover:

- fact recall;
- a dated format change with an explicitly superseded earlier fact;
- a corrected broadcast with its superseded version;
- source-host provenance;
- project-tag filtering;
- persistence across a seed process and a fresh query process.

The frozen file is the benchmark input. Change it only by creating a new
fixture version and reporting both versions separately; don't tune queries or
expected IDs after seeing a candidate's output.

## Reproduction

From the unified-agent repository root:

```sh
.venv/bin/python scripts/eval_memory_1458.py
```

The runner creates a temporary `UA_HOME`, writes through production
`memory_save`, applies fixed fixture timestamps and supersession links directly
to the isolated fixture database, rebuilds and freezes tag/master indexes,
then exits the seed process. A separate process runs production
`memory_search`.
It prints JSON with per-case recall@10, exclusions, query/result token counts,
latency, and aggregate p50/p95. `--out PATH` writes that JSON to a chosen
scratch path. No persistent memory database is opened or modified.

## Baseline limits

On a 2026-09-29 desktop v2 run, all six cases passed; the five positive
queries had recall@10 of 1.0, and the scope-isolation case returned no
out-of-scope result. The expected source host was returned on the provenance
case. Nearest-rank p50/p95 query latency was 30.565/40.166 ms; result
snippets and titles totaled 61 whitespace-delimited words. A control run
clearing supersession links exposed both old notes to the same two queries,
confirming the cases can catch that regression. These tiny runs cannot
support a capacity or production quality estimate. V1 results are not
combined with V2 results.

This first runner deliberately disables embeddings and exercises native FTS
fallback. It establishes a reproducible functional baseline and restart
persistence check; it does not measure the normal hybrid semantic ranker,
experience extraction, relevance injection into a live session packet, or
cost/latency under production load. Query/result whitespace token counts are
context-size proxies, not tokenizer counts. Local external API calls and
estimated external spend are zero by construction; CPU, storage, and model
inference costs are not priced. The tiny fixture cannot support statistical
claims about production recall or scale.

A follow-up scorecard should add a locally cached embedding run if its model
is already available, and report that separately. It must keep the corpus and
queries frozen, pin the model/runtime, and show both per-case failures and
ranking metrics. Do not download models or install dependencies as part of
this assessment.

## Hindsight and Supermemory comparison criteria

Compare only on the same fixture, with identical query text, scope, and
expected facts. Record candidate version/model/config, storage mode, required
services, and whether each run is offline. Score:

1. expected-fact recall@1/@5 and reciprocal rank;
2. temporal update and contradictory-broadcast handling, including whether
   old facts remain visible and how supersession is represented;
3. provenance returned with the fact and whether it distinguishes source from
   inference;
4. project isolation (no out-of-scope records in scoped results or injected
   context);
5. restart persistence and context packet token count;
6. p50/p95 query and write latency, measured locally on the same host;
7. external calls, model/token usage, and measured cost, with unknowns marked
   unknown rather than inferred from vendor headline numbers;
8. export/rollback path, deletion/forgetting semantics, and whether the system
   creates a second authoritative memory store.

No dependency installation, credential work, production-data import, or
second authoritative store is in scope. Hindsight retain/recall/reflect and
Supermemory extraction/profile/contradiction/forgetting are comparison
capabilities, not presumed wins. Candidate results remain **not measured**
until an adapter can run under these boundaries. Use the existing landscape
review in `Wiki/Agent-Memory-at-Scale.md` as context, not benchmark evidence.

## Decision boundary

**DEFER adoption or adaptation of either candidate now.** The frozen native
baseline has no failing case to justify a migration, and there is no measured
candidate result that establishes a gain. This does not close the option to
adapt a retain/recall/reflect or extraction/forgetting technique later. A
future comparison must run the same fixture and a larger representative set
within the permitted authority and credential boundary, report failures and
costs, and demonstrate export/rollback before changing the hub. If a candidate
requires network credentials or a new authority store to score, record that
as an integration constraint and defer its measurement. No data migration or
hub topology change follows from this decision.

## Follow-up: evidence-backed memory (task #1459)

Task #1459 adapted the retain/recall/reflect ideas inside the native store, as
this verdict requires: no candidate dependency and no second store. Notes can
be labelled `fact` or `inferred` (an inference must name evidence refs), carry
`valid_from`, supersede a changed fact through `memory_save(supersedes=...)`,
and link a disputed note with `contradicts`. Session packets label injected
notes and add bounded, attributable session-outcome `experience`. See
[memory-tasks-vault.md](../memory-tasks-vault.md#evidence-backed-notes-and-experience-task-1459).

`tests/test_memory_experience.py::test_retrieval_matches_frozen_baseline_through_supersedes`
seeds this frozen v2 fixture through the new `supersedes` path instead of the
runner's direct `superseded_by` writes, then requires the same per-case
pass/fail, recall@10 and exclusions as a fresh baseline run, and the same
exclusions in session-packet memory. The fixture and the runner are unchanged.
