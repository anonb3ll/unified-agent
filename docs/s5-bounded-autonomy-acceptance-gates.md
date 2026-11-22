# S5 / G5 — Bounded autonomy acceptance gates (task #373 Tier 2)

Written to be executable or directly observable, styled on
`docs/baseline-2026-08-07.md` §6. **This stage's gate is refused if any MUST
is unmet.** Nothing in Tier 2 (3.2 evidence contract, 3.3 budget/kill-switch,
3.4 auto-merge, 3.5 observability) may be built or merged until this document
exists and has been read — the sequencing itself is the lesson task #373's
handoff drew from S3: a criteria list written after the code exists isn't a
gate, it's a changelog.

## Numbering note — read before reusing "S5" or "G5" elsewhere

There is no other G5 in this repo. `docs/baseline-2026-08-07.md` defines
G0–G4 only; its own §6 states plainly "S5 (bounded autonomy) was renumbered
in planning and no longer depends on anything here" (line 244–245) — S5
survives the retirement of S3/S4 (federation) and is not resumed *from*
anything, it is defined fresh, here, against the current single-hub
topology. Do not confuse this with:

- **Gate E / Gate F** — an unrelated program (task #285, the desktop-migration
  single-hub cutover). Different lettering, different scope
  (`docs/desktop-migration.md`, `docs/gate-f-*.md`). Not a predecessor to S5.
- **Task #259 "S3.1"** — an older, unrelated "multi-agent OS" plan (task
  #250) that happens to reuse the string "S3.1". It built the
  `autonomous_cycles` table this gate depends on (see MUST 1 below) but is
  not part of the #276 roadmap's S-numbering.
- **Task #282** ("S5 — Bounded autonomy integration design") — filed under
  the now-retired #276/#280 federation roadmap and cancelled in cascade with
  S3/S4. Its own notes say: "If bounded-autonomy work is still wanted, it
  should be re-scoped as a new task against the actual single-hub topology
  rather than resumed here." Task #373 Tier 2 is that re-scoping. Nothing
  from #282's design is inherited by default — treat this document as the
  first S5 spec, not a continuation.

## What already exists (verified 2026-08-11, cite before re-deriving)

- `autonomous_cycles` (schema: `storage.py:568-611`) already has all four
  budget columns (`max_time_s`, `max_tokens`, `max_retries`, `max_cost_cents`
  + their `used_*` counterparts) and `autonomous_cycle_record_usage()`
  (`tools/cycles.py:286-355`) already compares all four correctly and
  symmetrically, flipping the cycle to `blocked` on any overage.
- **That comparison is the only enforcement that exists, and nothing calls
  it automatically.** `executor/daemon.py` has zero references to
  `autonomous_cycles` anywhere in its ~860 lines. Budget tracking today is
  entirely self-reported and opt-in — an executed agent that never calls
  `autonomous_cycle_record_usage` is invisible to the budget system for its
  entire run.
- No kill switch exists anywhere in the codebase. The closest things are a
  passive DB reaper (`registry.py:389-402`, relabels a stale session row —
  never signals a process) and a fixed, non-cancellable 300s subprocess
  timeout (`daemon.py:640-647`) unrelated to cycle budgets.
- No auto-merge to a remote exists anywhere. `reviewer.py`'s contract is
  "validate and hold" — it never pushes or merges on GitHub
  (`reviewer.py:3-5,26-28`). The one exception, `_merge_done_task`
  (`reviewer.py:783-859`), performs a **local** merge only, for tasks already
  `status=done`, and still refuses on red-tier files.
- The red-tier list is canonical in code (`RED_TIER_PATTERNS`,
  `reviewer.py:63-78`) and matches `COLLABORATION.md`'s Rule 5 prose exactly.
- `/api/attention` (`webui/api.py:597-750`) is a kind-tagged, extensible list;
  autonomous-cycle state does not appear in it today, and — unlike
  `roadmap_conflict`/`migration_gate` — isn't even flagged as a known gap
  (`api.py:27-43`). Adding a cycle-state kind fits the existing pattern.
- Task #372's evidence gate (`_evidence_required_error`, `tasks.py:134-153`)
  is narrowly scoped to `task_update(status="done")` for `[code-task]`-marked
  tasks only. It is not a general evidence contract — 3.2 extends the
  *pattern*, not the code path.

## MUST — 3.2/3.3 (evidence + budget/kill-switch)

1. MUST: budget usage is recorded automatically at a defined checkpoint in
   the real execution path (the executor daemon or its successor) — not
   solely by the executed agent voluntarily self-reporting. An agent must
   not be able to stay "invisible" to the budget system simply by never
   calling `autonomous_cycle_record_usage`.
2. MUST: exceeding any of the four budget dimensions (time, tokens, retries,
   cost) actually halts further execution of that cycle, not merely flips a
   `blocked` flag that nothing downstream checks before continuing. A test
   proves a cycle over its `max_tokens` (or any of the other three) cannot
   produce further billable/tool-calling work after the overage is recorded.
3. MUST: an on-demand kill switch exists that stops an in-flight autonomous
   run within a bounded, documented time window (state the number), without
   requiring a service restart or a permission-skip flag (Rule 6 — see MUST
   9). Invocable by a human at minimum; document whether/how an agent may
   invoke it and under what gate.
4. MUST: the kill switch is proven by a rehearsal script, structured like
   `scripts/gate_e_rollback_rehearsal.py` (isolated `UA_HOME`, real library
   calls not mocks, a `_check()`-style structured report, `try/finally`
   state restoration, JSON output + CLI exit code) — but unlike that script,
   it must actually spawn and then kill something, and assert (a) the
   process/cycle stops within the documented window, (b) the resulting DB
   state reads as "killed", distinct from "completed"/"blocked"/"failed",
   and (c) no real host process or state outside the rehearsal's own
   sandbox is touched.
5. MUST: every autonomous-cycle transition into `pr` or `completed` carries
   a verifiable evidence artifact — a commit SHA, PR URL, CI run link, or
   equivalent — never prose alone. This extends #372's `evidence_ref`
   pattern (`tasks.py`) to `autonomous_cycle_transition`
   (`cycles.py:164-283`); do not invent a second, incompatible evidence
   field.
6. MUST: capability-allowlist checking, which already gates
   `autonomous_cycle_start` and every `autonomous_cycle_record_usage` call
   (`cycles.py:91-99, 307-309, 325-331`), continues to gate whatever new
   automatic usage-collection call site MUST 1 adds. Do not introduce a new
   code path that records usage or advances a cycle without this check.

## MUST — 3.4 (auto-merge)

7. MUST: any new auto-merge path reuses `reviewer.py`'s existing
   `RED_TIER_PATTERNS` / `_is_red_tier()` check verbatim — not a
   reimplementation that could silently drift from the canonical list — and
   refuses/holds on a red-tier hit exactly as `_validate_and_hold` does
   today.
8. MUST NOT: land as a routine PR. Auto-merge-to-remote does not exist
   anywhere in this codebase today (the only existing auto-merge is local,
   scoped to already-`done` tasks). Introducing the first remote auto-merge
   capability is itself a red-tier-caliber change to this project's security
   posture and requires an explicit, separate, human-reviewed go/no-go
   before it lands — 3.4 must be presented and approved on its own, not
   bundled with 3.2/3.3/3.5.
9. MUST NOT: weaken `_validate_and_hold`'s existing "validate and hold"
   contract for anything red-tier or anything outside the auto-merge
   criteria's explicit green-path definition. The default for anything not
   provably green stays human-merge, unchanged.

## MUST — 3.5 (observability) and cross-cutting

10. MUST: autonomous-cycle state (at minimum: `blocked`, `failed`, and
    over-budget) is visible in `/api/attention` via a new kind-tagged item
    (matching `_identity_drift_items`/`_stale_agent_items`'s existing shape,
    `webui/api.py`), not a separate, undiscoverable surface a human has to
    already know to query.
11. MUST: no code path introduced for S5 issues a service restart
    (`systemctl --user restart ...`), a permission-skip flag
    (`--dangerously-skip-permissions`, `--yolo`, etc.), or any other
    Rule 6-prohibited action. A test or static check asserts these
    strings/actions never appear in the autonomous execution code path —
    Rule 6 says a self-deploying agent is a security incident, not a
    workflow optimization; this MUST makes that machine-checkable, not just
    a doc line trusted at review time.

## SHOULD

- SHOULD: budget-remaining is surfaced as a number (not an adjective) in
  `cycle_detail` and the Control Center cycle page (`/ui/runs/cycles/<id>`) — same discipline S1's gate 8 already
  requires for Browser latency.
- SHOULD: every kill-switch invocation is journaled/audited (who, when,
  which cycle, why) for post-incident review.
- SHOULD: the kill switch is a single documented command, mirroring the
  "single documented command" bar S1's gate 2 sets for Browser capture.

## If a MUST cannot be met

Document the exact blocker and mark G5 conditional at best, the same
discipline `docs/baseline-2026-08-07.md` §6 note 9 applies to G1 — an
autonomy feature shipped with self-reported-only budgets or no kill switch
is explicitly not G5, regardless of how much of 3.2–3.5 otherwise works.
