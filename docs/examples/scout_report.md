# Scout report example (task #1027)

FirstMate-style **scout** tasks investigate and leave a standalone vault
report — they do **not** open a PR branch.

## Intake

Stamp the task notes with either marker:

```text
[task-shape: scout]
```

or the short form:

```text
[scout]
```

## Executor finish

On success the Active Executor:

1. Refuses to daemon-commit / stamp a commit SHA as evidence.
2. Requires the agent output to include `[scout: report=<vault-rel-path>]`.
3. Stamps `[no-code]`, the report marker, `[scout: no-pr]`, and
   `[waiting-for: review]`.
4. Sets `evidence_ref` to `vault:<path>`.

## Report shape

Write under `00 - Inbox/` (vault write allowlist). Required sections:

1. Bottom line
2. Key findings
3. **Decision inventory** — numbered captain-actionable items
4. Caveats / open questions
5. Sources

Example decision inventory:

```markdown
## Decision inventory

1. **Decision:** Enable Tailscale Funnel for Grok Bot?
   - Options: yes / no / defer
   - Recommendation: no (keep private OAuth only)
   - Status: pending
```

Helpers live in `unified_agent.scout` (`build_scout_report`,
`render_decision_inventory`, `validate_scout_report`).

## Review

`scan_for_reviewable_tasks` returns `kind: scout` for these rows (they are
not dropped as ordinary `[no-code]` tasks). `review_task` validates the
report artifact and notifies the captain — it never merges.
