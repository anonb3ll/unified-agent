# Config revisions

Authority config (policy, profile overlay, approval rules, and the org charter)
keeps an immutable history. The store is the evidence ledger: append-only
`event_log` rows on channel `agent.config.revision`. There is no second table.
Live documents stay where they already live. A write through `commit` or
`rollback` records a revision. An edit made directly to the file does not;
`list` reports that as drift until `capture` records the live text.

| Kind | Live document | What a revision records |
|---|---|---|
| `policy` | `UA_POLICY_FILE` (default `~/.config/agent_hub/policy.yaml`) | The policy mapping, including rules that are not approval rules. `rate_limit` rules and `bind` blocks are part of this document; committing them is a policy revision. They do not change profile visibility. |
| `profile` | `profile_defaults` inside that same policy file | An overlay on the code defaults in `profiles.py` |
| `approval_rule` | `require_approval` rules in that file | Those rules only. `UA_APPROVAL_TOOLS` is snapshotted and never written |
| `org` | `src/unified_agent/org.yaml` | The charter file text |

A profile overlay may repeat an identity's code default or reduce its privilege
without granting a high-impact tool that default denies (`approval_resolve` and the rest of the developer or steward deny list). An allow-list overlay must stay inside that allow-list. Rank alone is not enough:
`operator` is below `developer` and still grants `approval_resolve`, so that
overlay is refused at commit and ignored if the file is edited by hand. A
same-rank swap or an escalation is refused the same way. Code allow and deny
sets are not writable at runtime.

## Operations

`config_revision_manage` (chief-of-staff only) takes `action`:

- `list` — newest-first summaries, plus `drift` and `latest_diff` against the live file
- `diff` — unified diff from `from_revision` to `to_revision` (`to_revision` omitted means live)
- `show` — one revision, including the document, after its stored hash is checked
- `capture` — record the live file when it has drifted; does not change the file
- `commit` — validate and write `document` through the existing store, then record it
- `rollback` — restore a prior revision

Rollback is a new revision. It does not delete history. The restore is requested
as tool `config_revision_rollback` with `force=True`, so it parks in the approval
queue even when `UA_APPROVAL_TOOLS` is empty. The requester cannot approve their
own restore (`config_revision_rollback` is in the separation-of-duties set).
The pass names the live file it was reviewed against. An edit that lands before
the restore is applied does not match that pass, so the restore is requested
again. If the target already matches live, the call returns `unchanged` and
does not consume a pass.

A commit that adds, removes, or edits `require_approval` rules is requested as
`config_revision_commit`, with the same force gate and separation of duties.
Commits that leave those rules untouched stay a chief-of-staff write. Do not
also add a `require_approval` policy rule for `config_revision_manage` unless
both gates are meant to pass.

A profile or approval-rule rollback patches only that slice of the current
policy file, so later unrelated policy edits survive. Rolling a policy or org
revision restores the stored file text. An empty policy raw deletes the file
and returns to the in-code defaults. An org rollback refuses an empty raw.

The first time a missing policy file is materialized, the in-code default rules
are included so an overlay-only write does not replace them with `rules: []`.

## Control Center

`GET /api/config-revisions?kind=policy` lists revisions. Optional `from` and
`to` add a diff. `POST /api/action/config-revision` with `{kind, revision}` is
the rollback. A pending gate returns HTTP 202 with `approval_id`. Agents → Organization
(`/ui/agents/org`, `?rev_kind=`) shows the list and that pending state.

The HTTP rollback runs the same agent-access and policy gates as the MCP
wrapper, then the inner `config_revision_rollback` pass. An approval-rule
commit uses `config_revision_commit`.

An overlay cannot remove every code-default chief-of-staff identity. A
downgraded chief of staff can still list and roll back; commit stays on the
effective profile, and `profile_activate` cannot climb back above the overlay.
A present empty policy file is refused. Comment-only policy and org diffs use
the stored file text.

Org revisions write `registry_path()`: the checked-in `org.yaml`, or
`UA_ORG_REGISTRY_PATH` when that is set. A git deploy replaces the package
file. An org edit that must survive deploy belongs on `UA_ORG_REGISTRY_PATH`,
which the deploy does not check out over.

`list` compares policy and org file text, not only the parsed mapping, so a
comment-only edit is drift until `capture`. When live matches the latest
revision, `latest_diff` is empty.
