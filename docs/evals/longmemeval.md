# LongMemEval retrieval scorecard (task #2145)

This scorecard asks whether `memory_search` finds a fact told in a past
conversation. It measures unified-agent's own retrieval path on the public
LongMemEval-S benchmark. Every number below came from the runs listed here,
and each one can be rerun with the commands at the end.

## Headline

Measured 2026-10-19. Runs A, C and D used the harness at commit `bc246b23`;
run B (`vault_query`) was rerun at `de84a1cc` after a fix to its note names
(see [Run B](#run-b-vault_query-all-470)). The strict metric
(GBrain's headline) asks that **every** evidence session for a question
appear in the top 5 results.

| Retrieval path | Questions | Strict recall@5 (all evidence) | Any evidence in top 5 |
|---|---|---|---|
| `memory_search`, hybrid (shipped default), one note per session | 120 of 470, stratified sample | **106/120 = 0.883** (95% CI 0.814 to 0.929) | 118/120 = 0.983 |
| `memory_search`, lexical legs only, one note per session | **all 470** | **388/470 = 0.826** (95% CI 0.789 to 0.857) | 456/470 = 0.970 |
| `vault_query`, one Markdown note per session | all 470 | 356/470 = 0.757 (95% CI 0.717 to 0.794) | 426/470 = 0.906 |

- **The hybrid figure is a sample, not a full-set number.** It covers 120 of
  the 470 answerable questions: 20 per question type, seed 2145. The full set
  did not fit the desktop compute budget (see [Wall time](#wall-time)). The
  sample weights the six types equally, but the full set has more of the hard
  types. Reweighting the per-type sample scores to the full set's type mix
  gives an **estimated strict recall@5 of 0.846** for hybrid over all 470. That
  is an extrapolation, not a measurement.
- **On the same 120 questions, the semantic leg adds two questions.** Hybrid
  scores 106/120 and lexical-only scores 104/120. The likely reason is
  truncation. A session note averages about 10,000 characters (evidence sessions run 7,000 to
  22,000), and the
  embedding model reads only the first 512 tokens (roughly 2,000 characters),
  so the vector describes the opening of the conversation and rarely reaches
  the evidence turn. Lexical FTS indexes the whole note.
- GBrain publishes 449/470 (95.5%) strict recall@5, recounted as 451/470
  (95.96%), on the same dataset file. The protocols differ, so the figures are
  not directly comparable. See [Comparison with GBrain](#comparison-with-gbrain).

Confidence intervals throughout are 95% Wilson score intervals for a binomial
proportion. They cover sampling error over questions only, not run-to-run
variation, which is zero here: every run is deterministic.

## Dataset

| Field | Value |
|---|---|
| Benchmark | LongMemEval ([paper](https://arxiv.org/abs/2410.10813), [code](https://github.com/xiaowu0162/LongMemEval)) |
| Hugging Face dataset | `xiaowu0162/longmemeval-cleaned`, revision `98d7416c24c778c2fee6e6f3006e7a073259d48f` (MIT) |
| File | `longmemeval_s_cleaned.json` (277,383,467 bytes) |
| sha256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Questions | 500. The 30 abstention questions (ids ending `_abs`) have no evidence session and are excluded, leaving 470 |
| Haystack | 38 to 62 sessions per question (mean 47.7), about 490,000 characters per question |
| Evidence label | `answer_session_ids`: 1 to 6 sessions per question (229 questions have 2, 71 have 3 or more) |

GBrain reports the same sha256 prefix (`d6f21ea9…`), so both scorecards use
the same file. The harness downloads the file on first run into
`~/.cache/ua-longmemeval/98d7416c/`, outside the repo, and refuses any file
whose hash differs. It parses the file as JSON only. The dataset is not
committed.

| Type | Answerable questions |
|---|---|
| single-session-user | 64 |
| single-session-assistant | 56 |
| single-session-preference | 30 |
| multi-session | 121 |
| knowledge-update | 72 |
| temporal-reasoning | 127 |

## Harness

`scripts/eval_longmemeval.py`. For each question it does the following:

1. It creates a fresh temporary `UA_HOME`, workspace and vault under the
   cache directory, and runs `post_init.init_db()`. It clears every `UA_*`
   variable except `UA_MEMORY_*` and `UA_EMBED_MODEL` (passed through so a
   ranking change can be studied), so the run never opens the hub database or
   reads host configuration.
2. It saves each haystack session through production `memory_save`, the same
   code path the MCP tool uses: dedup check, embedding, entity graph, FTS and
   index notes. The note looks like this:
   - title `Conversation on 2023/05/20 (Sat) 02:21`;
   - body `Conversation date: <date>`, then each turn as `user: …` or
     `assistant: …`, separated by blank lines;
   - `valid_from` set to the session date (UTC assumed), so the recency leg
     sees the conversation's own date;
   - tag `project:longmemeval-eval`, author `longmemeval-harness`.

   Production notes also carry a type tag (`fact`, `pref` or `decision`), per
   the memory conventions in the repo's AGENTS.md. These fixture notes carry
   only the project tag. Tags feed the entity leg and the tag index notes, so
   this is a known difference from production notes. Its effect is not
   measured; changing it would mean re-measuring run A.

   Notes carry no dataset session id. Ids such as `answer_280352e9` mark the
   evidence and would leak the label into lexical search. That is the leak
   GBrain corrected with opaque ids. The harness maps note ids back to session
   ids only for scoring.
3. It runs the question text through production `memory_search(query,
   limit=10)` with no tag filter.
4. It maps each returned note to its session and scores the top 5 and top 10.

The `vault_query` backend writes the same sessions as `Wiki/<stem>.md` files
(H1 title, then the same body) into a fresh vault, then calls production
`vault_query(question, max_results=10)`. It writes the files directly with
`Path.write_text`, not through production `vault_write`, so the vault write
path (sandbox, write-dir allowlist, locked-note guard) is not exercised; only
the ranking is. The stem is the note's haystack position with every
character separated, e.g. `s_0_0_4_7`. `vault_query` adds score when a query
term (2 or more letters or digits) is a substring of the stem, so the stem
must contain no two adjacent letters or digits. Ties keep sorted file order,
which is haystack order, so the run is deterministic.
`vault_search` is not scored. It is a regex line grep that returns matches in
file-walk order with no ranking, so recall@k is undefined for it.

**Turn granularity** (`--granularity turn`) saves one note per turn instead,
about 500 notes per question. Ranked items are cut at k, not distinct
sessions, so two turns from one session use two of the k slots. This is the
chunk-level protocol GBrain uses.

### Metrics

Over the session ids of the top k returned items:

- `recall_all@k`: 1 when every evidence session is in the top k (the strict
  metric);
- `recall_any@k`: 1 when at least one evidence session is in the top k;
- `recall_frac@k`: the share of evidence sessions in the top k;
- `MRR@10`: reciprocal rank of the first evidence session within the top 10.

`tests/test_eval_longmemeval.py` covers the scoring, sampling and note-rendering
helpers without the dataset or the model.

## Settings

| Setting | Value |
|---|---|
| Embedding model | `BAAI/bge-small-en-v1.5` (`config.DEFAULT_EMBED_MODEL`), loaded by fastembed as `Qdrant/bge-small-en-v1.5-onnx-Q`, 384 dims, truncated at 512 tokens |
| Ranking | Reciprocal rank fusion, `UA_MEMORY_RRF_K` = 60 |
| Legs and weights | lexical 1.0, semantic 1.0, entity 1.0, recency 0.2, graph 0.5 (defaults, no overrides) |
| Lexical leg | FTS AND of every token, then OR of content words when AND finds fewer than 10; cap 200 rows |
| Semantic leg | cosine floor 0.65, top 50 |
| Retrieval mode observed | `hybrid` on every hybrid question; `lexical_fallback` on every `--lexical` question |
| Search limit | 10; scored at k = 5 and k = 10 |
| Reranker, query expansion | none (not part of `memory_search`) |
| ONNX threads | 2 (`--threads 2` builds the same model with `threads=2`), `OMP_NUM_THREADS=2`, `nice -n 10` |
| Embedding cache | on (`--no-embed-cache` turns it off). It memoizes identical text only, and recorded 0 hits: each haystack re-dates its sessions, so no two note texts matched |
| Host | 4-core desktop shared with other agents (load average 5 to 15 during the runs) |

## Results

### Run A: `memory_search` hybrid, session notes, 120-question sample

`--per-category 20 --seed 2145`. Counts in parentheses.

| Type | n | all@5 | any@5 | frac@5 | all@10 | any@10 | MRR@10 |
|---|---|---|---|---|---|---|---|
| **overall (sample)** | 120 | **0.883** (106) | 0.983 | 0.941 | 0.950 (114) | 1.000 | 0.898 |
| single-session-user | 20 | 1.000 (20) | 1.000 | 1.000 | 1.000 (20) | 1.000 | 0.917 |
| single-session-assistant | 20 | 1.000 (20) | 1.000 | 1.000 | 1.000 (20) | 1.000 | 1.000 |
| single-session-preference | 20 | 0.900 (18) | 0.900 | 0.900 | 1.000 (20) | 1.000 | 0.672 |
| multi-session | 20 | 0.750 (15) | 1.000 | 0.897 | 0.900 (18) | 1.000 | 0.967 |
| knowledge-update | 20 | 0.900 (18) | 1.000 | 0.950 | 0.950 (19) | 1.000 | 0.975 |
| temporal-reasoning | 20 | 0.750 (15) | 1.000 | 0.898 | 0.850 (17) | 1.000 | 0.858 |
| *reweighted to the 470 type mix (estimate)* | | *0.846* | *0.994* | *0.932* | *0.926* | *1.000* | |

### Run C: `memory_search` lexical legs only, session notes, all 470

`--full --lexical`. The semantic leg is disabled. FTS, entity, recency and
graph legs run as shipped. This is the only full-set `memory_search` figure.

| Type | n | all@5 | any@5 | frac@5 | all@10 | any@10 | MRR@10 |
|---|---|---|---|---|---|---|---|
| **overall** | 470 | **0.826** (388) | 0.970 | 0.909 | 0.900 (423) | 0.989 | 0.851 |
| single-session-user | 64 | 0.984 (63) | 0.984 | 0.984 | 1.000 (64) | 1.000 | 0.856 |
| single-session-assistant | 56 | 1.000 (56) | 1.000 | 1.000 | 1.000 (56) | 1.000 | 0.790 |
| single-session-preference | 30 | 0.900 (27) | 0.900 | 0.900 | 0.967 (29) | 0.967 | 0.629 |
| multi-session | 121 | 0.645 (78) | 0.967 | 0.829 | 0.793 (96) | 0.984 | 0.862 |
| knowledge-update | 72 | 0.958 (69) | 1.000 | 0.979 | 0.986 (71) | 1.000 | 0.939 |
| temporal-reasoning | 127 | 0.748 (95) | 0.953 | 0.868 | 0.843 (107) | 0.984 | 0.868 |

### Run B: `vault_query`, all 470

`--full --backends vault_query --granularity session,turn`. No embeddings.
Measured at `de84a1cc`, run twice with identical rankings.

An earlier run at `bc246b23` named notes `s-<10 random hex>`. Hex can spell
query words ("be", "add", "face"), which `vault_query` scores against the
stem, so that run was non-deterministic and slightly inflated. The overall
strict counts came out the same (356 and 249); a few per-type figures,
any@5 and turn-level all@10 moved by 1 question each. The figures below replace it.

| Type | n | session all@5 | session any@5 | session all@10 | turn all@5 | turn any@5 | turn all@10 |
|---|---|---|---|---|---|---|---|
| **overall** | 470 | **0.757** (356) | 0.906 | 0.849 (399) | 0.530 (249) | 0.796 | 0.668 (314) |
| single-session-user | 64 | 0.906 | 0.906 | 0.984 | 0.812 | 0.812 | 0.906 |
| single-session-assistant | 56 | 0.821 | 0.821 | 0.875 | 0.714 | 0.714 | 0.732 |
| single-session-preference | 30 | 0.600 | 0.600 | 0.800 | 0.433 | 0.433 | 0.533 |
| multi-session | 121 | 0.620 | 0.950 | 0.727 | 0.289 | 0.793 | 0.479 |
| knowledge-update | 72 | 0.958 | 0.986 | 0.986 | 0.708 | 0.958 | 0.903 |
| temporal-reasoning | 127 | 0.709 | 0.929 | 0.819 | 0.457 | 0.819 | 0.598 |

`vault_query` scores lexical seeds by substring counts with no stopword list,
so words like "what" and "did" add score, and the PageRank walk has no links
to follow in a haystack with no wikilinks. It trails `memory_search` lexical
by 7 points at the session level.

### Run D: `memory_search` hybrid, turn notes, 18-question sample

`--per-category 3 --seed 2145 --granularity turn`. About 500 notes per
question and 86 s of ingest per question, so the sample is small.

| Type | n | all@5 | any@5 | frac@5 | all@10 | any@10 |
|---|---|---|---|---|---|---|
| **overall** | 18 | 0.556 (10) | 0.833 | 0.700 | 0.722 (13) | 0.889 |
| single-session-user | 3 | 0.667 | 0.667 | 0.667 | 1.000 | 1.000 |
| single-session-assistant | 3 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| single-session-preference | 3 | 0.667 | 0.667 | 0.667 | 0.667 | 0.667 |
| multi-session | 3 | 0.333 | 1.000 | 0.700 | 0.667 | 1.000 |
| knowledge-update | 3 | 0.667 | 1.000 | 0.833 | 1.000 | 1.000 |
| temporal-reasoning | 3 | 0.000 | 0.667 | 0.333 | 0.000 | 0.667 |

On the same 18 questions, session-level lexical (run C) scores 12/18 strict
recall@5 against 10/18 here. Turn notes lose because several turns from one
session fill the five slots, and a single turn often lacks the words of the
question. Session notes are the better granularity for this store.

### Chunked embeddings (task #2182)

The hybrid numbers above embed only the first 512 tokens of each session.
`UA_MEMORY_CHUNK_EMBEDDINGS` also stores overlapping windows of about 400
tokens (1,600 characters, 320 of overlap, at most 16) for a note longer than
about 2,000 characters, and scores the note by the best of its single vector
and those windows. The 120-question run did not fit a second time on this
host, so the comparison is the same stratified draw at 5 questions per type
(seed 2145, 30 questions, hybrid session notes, `--threads 2`). The before
command sets `UA_MEMORY_CHUNK_EMBEDDINGS=0`. The after command sets it on.

```sh
# Before: single truncated vector, flag off (26/30). About 13 min.
UA_MEMORY_CHUNK_EMBEDDINGS=0 OMP_NUM_THREADS=2 \
  nice -n 10 uv run python scripts/eval_longmemeval.py \
  --per-category 5 --seed 2145 --backends memory_search --threads 2
# After: chunked embeddings (27/30). About 47 min.
UA_MEMORY_CHUNK_EMBEDDINGS=1 OMP_NUM_THREADS=2 \
  nice -n 10 uv run python scripts/eval_longmemeval.py \
  --per-category 5 --seed 2145 --backends memory_search --threads 2
```

| Path | n | all@5 | any@5 | frac@5 | all@10 | MRR@10 | Wall |
|---|---|---|---|---|---|---|---|
| single vector (flag off) | 30 | 0.867 (26) | 0.967 | 0.936 | 0.967 (29) | 0.869 | 751 s |
| chunked embeddings (flag on) | 30 | **0.900 (27)** | 0.967 | 0.944 | 0.967 (29) | 0.914 | 2,844 s |

Per type, strict recall@5 moved only for multi-session (0.800 to 1.000). The
other five types were unchanged: user 0.800, assistant 1.000, preference
1.000, knowledge-update 1.000, temporal-reasoning 0.600. The gained question
is `e3038f8c` (multi-session): its evidence sessions sit thousands of
characters in, and the chunk scores cleared the 0.65 floor where the opening
vector did not. Two questions that were already hits moved their first
evidence from rank 3 to rank 1 (`95bcc1c8`, `6b7dfb22`). No question scored
worse. The three remaining misses are `ccb36322` (still rank 6; the best
chunk stayed under 0.65), `gpt4_45189cb4` and `6613b389`.

That gain is why the flag defaults on. The cost on this sample is ingest:
about 95 s per question instead of 25 s, because each session of about
10,000 characters becomes several vectors. A synthetic store of 48 notes of
10,000 characters grew by about 49 KB per note (about 6 KB per chunk, 8
chunks each), measured with the vec0 mirror included.

### Same-question comparison (run A's 120 questions)

| Path | strict all@5 | any@5 | all@10 |
|---|---|---|---|
| `memory_search` hybrid, session | 106 (0.883) | 0.983 | 0.950 |
| `memory_search` lexical, session | 104 (0.867) | 0.967 | 0.933 |
| `vault_query`, session | 93 (0.775) | 0.867 | 0.850 |
| `vault_query`, turn | 69 (0.575) | 0.742 | 0.692 |

## Misses

### Run A (hybrid, sample): the 14 strict misses at k = 5

"Evidence ranks" lists each evidence session's rank in the top 10 ("-" means
it was not in the top 10). The value after `@` is the character offset of the
first labelled answer turn in that session's note ("n/l" means no turn is
labelled). Offsets past about 2,000 fall outside the embedding window.

| Question | Type | Question text | Evidence ranks | Likely reason |
|---|---|---|---|---|
| `75832dbd` | preference | Can you recommend some recent publications or conferences that I might find interesting? | 8@43 | Preference is implicit; the query shares no specific words with the evidence |
| `d6233ab6` | preference | I've been feeling nostalgic lately. Do you think it would be a good idea to attend my high school reunion? | 9@1418 | Same: generic advice wording; the evidence session is outranked by other reunion and nostalgia talk |
| `a1cc6108` | multi-session | How old was I when Alex was born? | 1@3093, -@43 | Needs the user's age from one session and the birth from another; the second shares only "Alex" |
| `81507db6` | multi-session | How many graduation ceremonies have I attended in the past three months? | -@n/l, 6@n/l, 4@43, 2@43, 1@43 | Five evidence sessions must fill all five slots; two are unlabelled mentions |
| `2b8f3739` | multi-session | What is the total amount of money I earned from selling my products at the markets? | 9@43, 4@43, 3@43 | Three evidence sessions; one is outranked by unrelated "market" sessions |
| `3a704032` | multi-session | How many plants did I acquire in the last month? | 7@3133, 3@n/l, 1@4782 | Answer turns sit past the embedding window; aggregation across three sessions |
| `67e0d0f2` | multi-session | What is the total number of online courses I've completed? | 6@6678, 1@12394 | Both answers deep in long sessions; the second ranks just outside the top 5 |
| `3ba21379` | knowledge-update | What type of vehicle model am I currently working on? | 1@43, 8@3475 | The updated fact sits past the embedding window; the recency leg (weight 0.2) does not lift it enough |
| `0977f2af` | knowledge-update | What new kitchen gadget did I invest in before getting the Air Fryer? | -@2321, 1@2517 | The earlier-gadget session never names the Air Fryer; answer just past the window |
| `2ebe6c92` | temporal-reasoning | Which book did I finish a week ago? | -@43, 1@43 | "A week ago" needs date arithmetic against the question date; retrieval has no notion of it |
| `gpt4_93159ced` | temporal-reasoning | How long have I been working before I started my current job at NovaTech? | 3@6429, -@3387 | The prior-job session does not name NovaTech; answer past the window |
| `gpt4_59149c78` | temporal-reasoning | I mentioned that I participated in an art-related event two weeks ago. Where was that event held at? | 2@n/l, -@10562 | Relative date, and the answer is 10,000 characters into the session |
| `gpt4_45189cb4` | temporal-reasoning | What is the order of the sports events I watched in January? | 2@6213, 5@2155, 7@43 | Three sessions; "January" is a date constraint, not a word in the notes |
| `gpt4_f420262d` | temporal-reasoning | What was the airline that I flied with on Valentine's day? | 4@n/l, 3@n/l, 1@43, 6@43, 2@n/l | Five evidence sessions for five slots; one ranks 6th |

Patterns: 12 of the 14 misses still return at least one evidence session in
the top 5. The failures come from (1) questions whose evidence spans three to
five sessions that must all fit in five slots, (2) answer turns past the
512-token embedding window, (3) relative dates ("a week ago", "in January")
that need the question date, and (4) preference questions with no lexical
overlap.

### Run C (lexical, all 470): the 82 strict misses at k = 5

`*` marks a question where no evidence session reached the top 10.

- **multi-session (43):** `0a995998` `6d550036` `gpt4_59c863d7` `b5ef892d`
  `3a704032` `gpt4_d84a3211` `aae3761f` `gpt4_f2262a51*` `gpt4_a56e767c`
  `46a3abf7` `28dc39ac` `gpt4_2f8be40d` `2e6d26dc` `gpt4_15e38248` `88432d0a`
  `gpt4_7fce9456` `d682f1a2` `2ce6a0f2` `gpt4_d12ceb0e` `00ca467f`
  `gpt4_31ff4165` `9d25d4e0` `129d1232` `gpt4_194be4b3` `a9f6b44c`
  `gpt4_ab202e7f` `gpt4_731e37d7` `10d9b85a` `e3038f8c` `2b8f3739` `1a8a66a6`
  `gpt4_372c3eed` `81507db6` `d3ab962e` `3c1045c8` `a1cc6108` `67e0d0f2`
  `ba358f49` `60159905` `bc149d6b` `157a136e` `c18a7dc8` `8e91e7d9*`
- **temporal-reasoning (32):** `b46e15ed` `0bc8ad92` `gpt4_7f6b06db`
  `gpt4_a1b77f9c` `gpt4_7abb270c` `gpt4_45189cb4` `gpt4_e061b84f`
  `gpt4_d6585ce8` `gpt4_f420262c` `eac54adc` `2ebe6c92` `gpt4_e061b84g`
  `b46e15ee` `gpt4_d6585ce9` `gpt4_1e4a8aec*` `gpt4_f420262d` `gpt4_59149c78`
  `gpt4_4929293b` `gpt4_fa19884d` `9a707b82` `eac54add` `4dfccbf8`
  `0bc8ad93` `6e984302` `gpt4_8279ba03*` `gpt4_68e94288` `a3838d2b`
  `gpt4_93159ced` `gpt4_9a159967` `gpt4_93f6379c` `gpt4_2f56ae70` `6613b389`
- **single-session-preference (3):** `75832dbd` `06f04340*` `d6233ab6`
- **knowledge-update (3):** `945e3d21` `3ba21379` `0977f2af`
- **single-session-user (1):** `ccb36322`

Multi-session and temporal-reasoning account for 75 of the 82 misses. That is
the same split as GBrain's 19 misses (9 multi-session, 10 temporal-reasoning).
For per-question ranks, rerun with `--out` and read `runs.*.questions[]`; each
row carries `ranked_sessions` and `evidence_diag`.

## Comparison with GBrain

GBrain's published figure ([gbrain-evals](https://github.com/garrytan/gbrain-evals),
`docs/benchmarks/2026-10-04-longmemeval-opaque-followups.md`) is 449/470
strict recall@5 in its release configuration, recounted as 451/470 (95.96%)
with opaque session ids. Its per-type strict hits are knowledge-update 72/72,
multi-session 112/121, single-session-assistant 56/56,
single-session-preference 30/30, single-session-user 64/64 and
temporal-reasoning 117/127.

The two figures share the dataset file (same sha256), the 470-question
denominator, the strict all-evidence-in-top-5 metric, and opaque note ids.
They differ in these ways:

1. **Sample versus full set.** Our hybrid figure is 120 questions. Our only
   full-set `memory_search` figure (run C) is lexical-only.
2. **Embedder.** GBrain uses OpenAI `text-embedding-3-large` (1,536 dims) over
   chunks. Runs A–D use local `bge-small-en-v1.5` (384 dims) over one vector
   per note, truncated at 512 tokens. Chunked embeddings (task #2182) add
   overlapping windows for long notes; the measurement is the 30-question
   sample in [Chunked embeddings](#chunked-embeddings-task-2182), not a
   re-run of the 120.
3. **Reranker.** GBrain's release configuration adds a Voyage reranker
   (434/470 without it in the recount). `memory_search` has none.
4. **Unit of retrieval.** GBrain returns five chunks. Our session-level run
   returns five whole sessions, which is the easier setting for strict recall.
   Our turn-level run is closer to chunks and scores lower.
5. **Tuning.** GBrain states its configuration was chosen on these 470
   questions. `memory_search` ran with its shipped defaults, untuned on this
   benchmark.
6. **Rendering.** Session text and date formatting differ in detail (ours:
   a `Conversation date:` header and `role:` prefixes).

Per type, the gap is concentrated where GBrain is also weakest.
Multi-session is 0.750 in our hybrid sample and 0.645 in our full lexical
run, against 0.926 for GBrain. Temporal-reasoning is 0.750 and 0.748, against
0.921. The single-session and knowledge-update types are within about 10 points
of GBrain in our runs. The data points to three levers: embedding more than the
first 512 tokens of a note (chunked vectors, now the default; see the
30-question measurement above), using the question date for
relative-time questions, and reranking the fused candidates.

## Wall time

| Run | Questions | Wall | Per question |
|---|---|---|---|
| A: hybrid, session | 120 | 3,847 s | 32 s (28 s ingest, 0.19 s search) |
| B: `vault_query`, session and turn | 470 (×2) | 130 s | 0.14 s |
| C: lexical, session | 470 | 2,498 s | 5.3 s (3.7 s ingest) |
| D: hybrid, turn | 18 | 1,579 s | 88 s (86 s ingest) |
| Chunked, flag off, 5 per type | 30 | 751 s | 25 s |
| Chunked, flag on, 5 per type | 30 | 2,844 s | 95 s |

Ingest is dominated by embedding: about 0.24 s per 512-token session on 2
ONNX threads, with one embedding per `memory_save`. A quiet-host smoke run
measured 17 s per question for hybrid session notes. At that rate the full
470 needs about 2.2 hours; at the rate measured here it needs about 4.2
hours. Both exceed the one-hour budget, so the hybrid run is a sample.

## Rerun

From the repository root, after `uv sync`. The first run downloads the
dataset (277 MB) and the fastembed model.

Runs A, C and D below were measured before chunked embeddings existed.
Prefix A and D with `UA_MEMORY_CHUNK_EMBEDDINGS=0` to reproduce that
single-vector path. `--lexical` (run C) sets the same flag itself, so a
lexical rerun writes no chunk rows and does not load the embedding model.
That is the path behind the published 40 min / 2,498 s figure; the flag
does not change it.

```sh
# Run A: hybrid sample, single vector (about 35 to 65 min on 4 cores)
UA_MEMORY_CHUNK_EMBEDDINGS=0 OMP_NUM_THREADS=2 \
  nice -n 10 uv run python scripts/eval_longmemeval.py \
  --per-category 20 --seed 2145 --backends memory_search --out lme-A.json
# Run B: vault_query, full set (about 2 min)
uv run python scripts/eval_longmemeval.py --full --backends vault_query \
  --granularity session,turn --out lme-B.json
# Run C: lexical-only memory_search, full set (about 40 min; no chunk embeddings)
nice -n 10 uv run python scripts/eval_longmemeval.py --full --lexical \
  --backends memory_search --out lme-C.json
# Run D: hybrid turn notes, single vector, small sample (about 25 min)
UA_MEMORY_CHUNK_EMBEDDINGS=0 OMP_NUM_THREADS=2 \
  nice -n 10 uv run python scripts/eval_longmemeval.py \
  --per-category 3 --seed 2145 --backends memory_search --granularity turn --out lme-D.json
```

**Full-set hybrid, on a bigger host** (not run for this scorecard):

```sh
uv run python scripts/eval_longmemeval.py --full --backends memory_search \
  --threads 0 --out lme-full-hybrid.json
```

`--threads 0` lets ONNX use every core. Questions are independent, so the
set can also be split with `--ids` across processes and the JSON reports
merged. `--data PATH` uses an already-downloaded copy, which is still
hash-checked. `--work-dir` keeps the scratch stores for inspection.

## What this does not show

- **No answer accuracy.** This scores retrieval only: whether the evidence
  comes back. It does not test whether a reader model answers correctly from
  it.
- **No full-set hybrid number.** The 0.846 reweighted figure is an estimate
  from 20 questions per type. The chunked-embedding comparison is a further
  cut of that draw, 5 per type, not a second 120-question run.
- **Haystack size.** About 48 notes per question is a small store; the hub's
  `memory.db` holds more notes, with tags, authors and supersession that this
  benchmark does not exercise. Scale behaviour is covered separately by
  `scripts/bench_memory_search_scale.py`.
- **`vault_search`** is unranked regex search, so it is not scored.
