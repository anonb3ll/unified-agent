# Recording decisions

How-to. This page defines where a human decision is written down so that agents
act on it. It exists because the project's recurring failure is not slow
decisions — it is decided things that no agent can see.

A decision made in a chat session is not recorded. The transcript is not read by
the next agent, and a cloud session's container (with its `memory.db`) is
reclaimed after inactivity. Only pushed files and the desktop database survive.

## The four channels

Each channel is read by something different. Choosing the wrong one is how a
decision gets made and the work still does not move.

| Channel | Read by | Use it for | Unblocks automation? |
|---|---|---|---|
| `memory_save(tags=["decision", "project:<name>"], author="Operator")` | Agents at session start, via `memory_search` | Conceptual decisions: conventions, "don't do X because Y", design choices | No |
| `memory_save(tags=["broadcast", …])` | Agents at session start, high salience, sorts first | A decision that changes what an agent should do in its *next* session. Only a verified human identity (`Desktop_Person`) can save the `broadcast` tag or salience above 0.8 | No |
| Task annotations in a task's notes | The review pipeline, on a `*/15` cron | Releasing one specific blocked task | **Yes** |
| `approval_resolve(id, decision, resolved_by, reason)` | The approval queue, at call time | One specific tool call that parked | **Yes** |
| A `docs/` page, committed | Agents reading the gate, and humans | Architectural go/no-go, gate closure, scope | No — but it is the only durable record |

### Conceptual decisions go to memory

[COLLABORATION.md](COLLABORATION.md) Rule 3 is the standing rule: decisions go
to memory, not just chat. Tag `decision` plus `project:<name>`, and pass `author="Operator"` so
the note carries an owner — the database is shared, and `memory_save` refuses a
full-replace of another author's note without `overwrite=True`. Agents are
instructed to open a session with
`memory_search("<project>", tags=["project:<name>"])`.

Use `broadcast` in addition when the decision changes an agent's immediate
behavior rather than its background knowledge. Saving that tag, or a salience
above 0.8, requires the verified identity to be human (`Desktop_Person`).
The `author` argument is not enough.

### Blocked tasks are released by annotation, not by prose

These strings are parsed, not conventions. A memory note saying "approved" does
not release a task; replacing the annotation does.

| Annotation | Meaning | Cleared by |
|---|---|---|
| `[waiting-for: review]` | Ready for automated review | The pipeline |
| `[waiting-for: plan-approval]` | Needs your sign-off on a plan | Replace with `[plan: approved @ <ts>]`, or remove it |
| `[review: red-tier]` | Passed tests, touches sensitive files | Human approval |
| `[review: rejected]` | Validation failed | Rework |
| `[fyi]` | Informational | Nothing |

The plan-approval path is the clearest example of the split: the pipeline's
`review_plan_approvals` step loads the plan read-only, summarizes it, and
notifies you once per plan-state fingerprint. **It never approves.** The task
leaves the scan when you replace the annotation.

Red-tier escalation covers `config.yaml`, `.env`, `SOUL.md`, `SKILL.md`,
`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `security*`, `safety.py`, `server.py`,
`deploy/`, and `hooks/`.

### Approvals are one-shot, and narrow

`approval_resolve` clears one exact tool-plus-payload pair, and a denial blocks
retries with the reason you recorded. It is for "yes, run that command." It is
never the record of an architectural decision. Each hub adds tools via
`UA_APPROVAL_TOOLS`; where that is unset, the baseline `delegate` parks agent
submissions (unless `UA_APPROVAL_BASELINE=0`). Verified human web submissions
skip that delegate gate. Forced gates for work-product release and production
actions apply independently of the configured tool list.

### Gate decisions belong in the gate document

A gate lives in a `docs/` file, and that file is what an agent reads to learn the
gate exists. A decision recorded only in memory leaves the document saying
"awaiting decision", and the next agent correctly treats it as open. This is the
drift the dated snapshot assessment-2026-08-12.md §3.5
diagnosed.

The model to copy is the federation retirement entry in `CHANGELOG.md`. It
records the decision, names the evidence, notes the design had been filed twice
and built once, states plainly **"Do not build it"**, and says what is *not*
retired. A future agent cannot mistake it for open work.

## Template

Write the record where the routing table sends it. Keep the shape:

```markdown
## Decision: <what was decided>

**Date:** YYYY-MM-DD
**Decided by:** <name>
**Status:** decided | reversed by <link>

**Decision.** One sentence, in the imperative. "Do not build X." "Cut over to Y."

**Why.** The reasoning a future reader needs to not re-litigate it — including
the cost accepted, not only the benefit claimed.

**Scope.** What this does *not* cover, so the record is not over-read.

**Evidence.** Commits, test runs, or documents the decision rests on.
```

Then, in the same change: append a `vault_journal` line for the timestamped
trail, and `memory_save` a short form tagged `decision` so it is searchable.

## Open decisions

Three programs are at genuine gates. None is blocked on engineering. Each row
names exactly what a decision would consist of, so none needs re-derivation.

### 1. S5 / 3.4 auto-merge

- **Question:** may the system merge its own work on the green path?
- **Record lands in:** [s5-bounded-autonomy-acceptance-gates.md](s5-bounded-autonomy-acceptance-gates.md),
  then `CHANGELOG.md` if the answer is no.
- **Constraint:** the gate says this "requires an explicit, separate,
  human-reviewed go/no-go" and "MUST NOT land as a routine PR."
- **State:** groundwork shipped (`autonomous_cycle_green_path_eligibility`,
  `1bbe1cb`). The next natural commit is the one the gate forbids taking
  casually.
- **Standing recommendation** (`assessment-2026-08-12.md` §3.5, a superseded
  2026-08-12 snapshot whose counts and state are historical): *no, for now*.
  Nothing is bottlenecked on it; the cost is that the strongest human checkpoint
  stops being one. Revisit when green-path eligibility has its own soak record.
- **Not this question:** the operator's 2026-09-23 ruling lets the *claiming agent* merge
  its own PR on exact-head green CI plus one independent review with no unresolved blocking finding, review threads resolved
  ([automation.md](automation.md#policy-validate--hold)). That is an agent acting on
  its claimed task, not the system merging on the green path; this gate is untouched.

### 2. Gate F retirement

- **Question:** what scope of the laptop vault-namespacing layer is retired?
- **Record lands in:** [gate-f-retirement-proposal.md](gate-f-retirement-proposal.md) §7.
- **State:** **the proposal's §4 removal scope has already shipped in full** —
  verified against the tree on 2026-08-16 and recorded in that document's §7.
  The decision is now a documentation reconciliation, not a code deletion.
- **Remaining precondition:** §5 item 2 — whether existing notes under
  `00 - Inbox/Laptop` and `Logs/Laptop` should be migrated or left in place.
  That question is real and unaffected by the code already having landed.
- **2026-09-05 (UA #761):** the last residual reader of the namespace,
  `vault_briefing_digest`'s laptop-journal staleness (`laptop_stale_hours` /
  `stale` / `escalate`), is removed. The operator confirmed every machine now
  journals to vault root `Logs/` against the one shared hub, so the field
  reported a dead path as perpetually escalated. The `Logs/Laptop/{date}.md`
  merge is kept for historical `date=` reads only.

### 3. Release boundary production cutover

- **Question:** does the service run from an immutable release artifact?
- **Record lands in:** [deploy-boundary-proposal.md](deploy-boundary-proposal.md) §5.
- **State:** machinery built and green (`release.py`, `deploy/build_release.py`,
  `deploy/release_rollback.py`, `tests/test_release_boundary.py`). §5 is a
  runbook, not a proposal.
- **What the decision consists of:** your explicit direction, plus running
  `build_release.py … --authorize` and repointing the unit's `ExecStart`. The
  document already states no automatic cutover occurs without it.

## Checks

```bash
uv run pytest -q tests/test_docs_decisions.py
uv run ruff check .
```
