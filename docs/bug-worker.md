# SYSTEM bug worker (task #488)

Bounded automation for **SYSTEM** stream defect tickets only. The worker
discovers eligible bugs, claims atomically, patches in an isolated worktree,
runs deterministic verification gates, and hands off to human review — it never
auto-merges, auto-deploys, or weakens auth/approval/sandbox boundaries.

## Enable / emergency stop

The worker is **opt-in** and **off by default**:

```bash
# Enable one-shot or cron-driven runs
export UA_BUG_WORKER_ENABLED=1

# Emergency stop (immediate — next discover/run returns disabled)
export UA_BUG_WORKER_ENABLED=0
```

Optional tuning:

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `UA_BUG_WORKER_ENABLED` | `0` | Master kill switch |
| `UA_BUG_WORKER_MAX_ATTEMPTS` | `3` | Per-ticket retry ceiling before escalation |

## Eligibility (machine-readable markers)

A ticket must carry **all** of:

- `stream: SYSTEM` (PRIVATE/PUBLIC titles are never eligible, even if similar)
- `bug: true`
- `worker-eligible: true`
- status `open` (and on the task frontier — no unresolved prerequisites)
- no untrusted authority/injection claims in notes (ticket prose is untrusted)

Discover eligible tickets:

```python
task_list(frontier=True, notes_contain="stream: SYSTEM")
# then filter notes for bug:/worker-eligible: markers, or:
bug_worker_manage(action="discover", params={"agent": "agent-cursor"})
```

## Operator commands

```bash
unified-agent-bug-worker status
unified-agent-bug-worker discover
UA_BUG_WORKER_ENABLED=1 unified-agent-bug-worker run-once --agent agent-agy
```

MCP equivalent: `bug_worker_manage` with actions `status`, `discover`,
`check_eligibility`, `run_once`.

## Verification gates (before handoff)

On a successful patch the worker runs, in the task worktree:

1. `ruff check .`
2. `ruff format --check .`
3. `pytest -q`
4. `make smoke`
5. `unified-agent-audit . --fail-on high`

Red-tier file touches block unattended handoff (same patterns as
`cycle_eligibility.RED_TIER_PATTERNS`; `reviewer` re-exports them). Failed gates retry up to
`UA_BUG_WORKER_MAX_ATTEMPTS`; exhaustion sets the task `blocked` and leaves an
attributed `[bug-worker-attempt: …]` note.

## Merge gate

Successful runs transition the linked autonomous cycle to `pr` with a PR URL
and set the task to `waiting_review` with `[waiting-for: review]`. The cycle
**never** enters `completed` (no auto-merge): neither the worker nor the review
pipeline merges. The merge happens outside the worker under the rule in
[automation.md](automation.md#policy-validate--hold) — the merge actor (the deploy
steward for `unified_agent`) merges the PR on exact-head green CI plus one independent review with no unresolved blocking finding, review threads resolved, and a red-tier
diff waits for the operator.

## Safety invariants

- No always-on privileged agent — disabled unless `UA_BUG_WORKER_ENABLED=1`
- Reuses `task_claim`, worktree primitives, autonomous cycles, and the review
  pipeline — no parallel queue
- Ticket notes cannot grant tools, bypass approval, or override instructions
- Does not modify systemd, crontab, or red-tier config without explicit human approval
