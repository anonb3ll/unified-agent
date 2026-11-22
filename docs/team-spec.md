# Team specs: declare a team, preview it, boot it

Task #1736. Pattern source: OpenRig's RigSpec and `rig up --plan` (pattern
only, no runtime dependency). The cockpit write-up
`docs/audits/2026-10-01-openrig-competitive-assessment.md` is not in this
repository.

A team spec says who is on a team for a run and how each seat starts. The
operator writes it once, previews the boot plan, and boots the whole team
from one call or one Control Center button. Code: `src/unified_agent/team_policy.py`
(core layer: parsing and validation of the `teams:` block) and
`src/unified_agent/team_spec.py` (services layer: plan and boot).

## Where a spec lives

Specs live in the reviewed policy file (`UA_POLICY_FILE`, usually
`~/.unified_agent/policy.yaml`) under a top-level `teams:` block, keyed by
org office id. That makes them part of the `policy` config kind in
`config_revisions` (#1352):

- every change is a hash-chained revision with an actor and a diff;
- a commit is a chief-of-staff write (`config_revision_manage` authority);
- a rollback is approval-gated, as for every config kind;
- the policy validator refuses a spec that names an unknown office or roster
  label, or is malformed, on every path that records policy text: commit,
  rollback, capture, and an unchanged commit of a hand-edited file. A bad
  spec never becomes a revision.
- reads of the live file obey the same 256,000-byte limit as config
  revisions and are cached by file size and mtime.

Write a spec with `coordination_manage(action="team_spec_commit",
params={"team": ..., "spec": {...}})`, or commit the whole policy document
with `config_revision_manage`. A hand edit of the file is recorded as drift by
the next `capture`, like any other policy edit.

## Example: a two-seat Claude + Codex pair

```yaml
teams:
  platform-engineering:          # an existing org.yaml office id
    description: Builder and checker on different harnesses
    operating_profile: standard  # an operating profile name (#1732)
    mission_id: 1450             # optional goal id; its autonomy mode applies
    guidance: |
      Ship small PRs. Every PR names its task id.
    skills: [review-code]        # names in the active harness_config bundle
    seats:
      - label: agent-claude    # an org.yaml roster label; no new identity store
        harness: claude          # a key of acp_matrix.ACP_HARNESSES
        model: opus
        host: desktop            # a ua-node name or its device name
        workspace:
          repo: unified_agent    # relative to the node's worktree root, or absolute
          worktree: task         # task: claim, then .worktrees/task-<id>; shared: the checkout
        guidance: You review the exact head the builder pushes.
      - label: agent-codex
        harness: codex
        model: gpt-5-codex
        host: desktop
        workspace: {repo: unified_agent, worktree: task}
    edges:
      - {from: agent-claude, type: delegates_to, to: agent-codex}
      - {from: agent-claude, type: reviews, to: agent-codex}
      - {from: agent-codex, type: escalates_to, to: Desktop_Person}
```

Field rules (unknown keys are refused everywhere):

| Field | Rule |
|---|---|
| team id | An org.yaml office id. |
| `seats` | 1 to 16 seats, one per roster label. |
| `harness` | One of `claude`, `codex`, `gemini`, `copilot`, `cursor`, `opencode`, `hermes` (`team_policy.HARNESSES`, kept equal to `acp_matrix.ACP_HARNESSES` by a test). tmux is not a harness. `hermes` seats are refused in the plan for now: `acp:hermes` needs a `hermes_profile`, which specs do not carry. |
| `model` | Optional; letters, digits and `._:/@-`. |
| `host` | Optional; a node name or a device name. Without it, the label's live node that advertises the harness is used. |
| `workspace.repo` | Optional; absolute, or relative to a node worktree root. `~` and `..` are refused: the hub cannot expand a node's home. The node must report the checkout (see Workspaces below). |
| `workspace.worktree` | `task` (default) or `shared`. |
| `guidance` | Team and seat text, up to 4000 characters each; joined team first. |
| `skills` | Kebab-case names, up to 16. Delivered by `harness_config` bundles. |
| `edges` | `delegates_to`, `reviews`, `can_observe`, `collaborates_with`, `escalates_to`. Both ends are seats, except `escalates_to`, which may name any roster label. |
| `operating_profile` | A profile name; see below. |
| `mission_id` | Optional positive goal id. |

Edges are recorded, not enforced. Enforcing them as a communication policy
belongs to #1735. `delegates_to` sets the launch order: a seat starts after
every seat it delegates to, so a lead never hands work to a seat that is not
up. A `delegates_to` cycle is refused.

## Plan

`coordination_manage(action="team_up", params={"team": "platform-engineering"})`
(or `GET /api/teams/plan?team=platform-engineering`) returns a dry-run plan
and changes nothing. Pass `spec` instead of `team` to preview an uncommitted
draft. Per seat it shows the node, backend, model, launch cwd, guidance,
skills, edges, a hash of the startup prompt, and each readiness check:

| Check | Refusal code |
|---|---|
| Org office known | `unknown_office` |
| Label on the org.yaml roster | `unknown_label` |
| A node on `host` exists and runs as this label | `unknown_host`, `host_not_bound_to_label`, `no_node_for_label` |
| The node is live | `host_offline` |
| The node advertises `acp:<harness>` | `harness_unavailable_on_host` |
| The node reported the checkout (no auto-trust by name) | `workspace_unverified` |
| The checkout's real path stays inside its root (no symlink escape) | `workspace` |
| Not an `acp:hermes` seat | `hermes_profile_unsupported` |
| Label not revoked; lifecycle active | `revoked`, `lifecycle` |
| WIP under `UA_AGENT_WIP_MAX` (or the label's `UA_AGENT_WIP_OVERRIDES` cap) | `wip_cap` |
| Budget allows one more run (`budgets.evaluate_claim`) | `budget` |
| Trust preset allows a native launch | `trust_preset` |
| Every skill is in the active harness_config bundle | `skill_not_published` |
| The node reports those skills applied for that bundle version | `skill_not_applied` |
| Operating profile known (when validated) | `operating_profile` |
| Mission autonomy mode does not deny | `autonomy_deny` |

A mission whose mode would pause (the default `action-review` mode does) is
not a refusal: the plan shows `autonomy.decision: pause_for_human`, and boot
opens the mission review (see Boot).

### Workspaces

The hub never sees a node's filesystem, so it trusts only what the node
measured. At registration a node reports its worktree roots and the git
checkouts directly under them (`repos`), each with its resolved real path.
A seat's `workspace.repo` must match a reported checkout, and that
checkout's real path must stay inside its root's real path. A checkout the
node did not report is `workspace_unverified`; a symlink that leaves its
root is refused. Without `repo`, the seat starts in the first reported root.

The plan, the `team_up` approval and the queued node item also bind the
checkout's real path (`workspace.cwd_realpath`, task #1770), not only its
path string. Re-reporting a retargeted checkout changes the plan hash, and
at launch the node resolves the cwd again (`Path.resolve(strict=True)`) and
refuses the item as `cwd_realpath_changed` when it differs, even if the new
target is still inside the roots. A bare root (no `repo`) carries no bound
real path. The plan text shows the bound real path on each seat line (`cwd=<path> -> <real
path>` when they differ) and on a `binds cwd_realpath=` line, so the operator
approving the plan sees which checkout it binds.

A bound checkout also requires the node to advertise `cwd_realpath`. A seat
without an explicit `host` is placed on a node of its label that advertises it
when one exists. Team planning, dispatch approval/enqueue and claim refuse a
node without it; upgrade ua-node before booting those seats. Restarting or
resuming a bound item, or restoring it from a team snapshot (fresh, rebuilt or
resumed), re-binds the same real path. The node rechecks both the alias and the
resolved path immediately before calling the backend. This is a path binding,
not an inode pin: replacement at the same real path and filesystem changes
after that last check are not covered by an atomic descriptor-based launch.

### Skills

Skills reach a seat only through `harness_config` bundles. After each sync a
node reports, on its heartbeat, the bundle version it is in sync with and the
skills its skill-bearing surfaces (Codex and OpenCode config) deliver. A seat
is ready only when every declared skill is reported applied for the active
bundle version and the surface named for the seat's own harness delivers
skills (`codex` for a Codex seat, `opencode` for an OpenCode seat); otherwise
run `harness_config apply` on that node. Rules surfaces (`CLAUDE.md`,
`AGENTS.md`, `.cursorrules`) carry no skills, so a seat on `claude`, `cursor`
or another harness without a skill-bearing surface cannot declare skills. The
hub ignores a skill list that no in-sync, skill-bearing surface backs; the
node still self-reports, since the hub cannot read its disk.

The plan carries no timestamps. Two plans of the same spec against the same
hub state are identical, `plan_sha256` included, and `plan_text` is a
line-oriented rendering meant for `diff`. For each seat it shows what the boot
approval binds: the startup prompt hash, the guidance hash and text, and the
skills.

### Operating profile

`operating_profile` stores a name. When `unified_agent.operating_profiles`
(#1735) is importable and configured, the plan validates the name and refuses
an unknown one. Otherwise the plan reports the status as `unvalidated` and
says so in `operating_profile.note` and in `plan_text`.

## Boot

`team_up(team=..., plan=False)` (or `POST /api/action/team-up` with `team`
and the `plan_sha256` you reviewed) boots a committed spec. An inline draft
cannot boot.

1. Over HTTP, the same gates as the MCP call run first: agent access, the
   caller's profile ceiling on `coordination_manage(team_up)`, trust-preset
   admission and the policy gate. A denial is HTTP 403. (The `operator`
   profile exposes no consolidated managers, so an operator identity cannot
   boot from the Control Center; a chief-of-staff or developer identity can.)
2. The plan is recomputed. A refused plan returns `booted: false` before any
   approval is requested. A plan that no longer matches `plan_sha256` returns
   `stale_plan: true` (HTTP 409).
3. With a `mission_id`, the mission's autonomy mode is evaluated for this
   exact spec. A pause returns `autonomy_review` with the pending action id
   (HTTP 202); a human acknowledges it (`goal_manage` action `autonomy_review_action`), then
   boots again. When `team_up` is in `UA_APPROVAL_TOOLS`, the pause also opens
   the mission-bound `team_up` approval request (`global_approval_id` in
   `autonomy_review`); a human approves it before the review can be
   acknowledged. The mission review is spent at the first launch attempt, so a
   boot that launches nothing can be retried without a new review. A denial
   stops the boot.
4. One approval pass, bound to the exact seats, nodes, backends, cwds and
   prompt hashes, is requested under the tool name `team_up`. Until a human
   approves it the call raises `ApprovalPendingError` (HTTP 202 with
   `approval_id`); re-send the same request once approved.
5. Seats launch in edge order through the ua-node queue as ACP sessions
   (`node_queue.enqueue`, the #1329 path). The launcher is injectable
   (`team_spec.set_launcher_factory`) so tests use a double. Right before each
   enqueue, identity, lifecycle, WIP, budget and trust are read again; a seat
   that became refused after the preview is skipped as `refused`; a recheck
   that errors is that seat's `failed` outcome. A seat
   whose delegate did not launch is `not_started`; independent seats still
   start.
6. Each seat gets one run record on the `agent.team.run` evidence channel,
   linked to the `team_run_id` (`team_run_started`, then `seat_launched`,
   `seat_refused`, `seat_failed` or `seat_not_started`). A ledger failure on
   a seat record is reported on that seat (`record_error`) and the boot
   continues. If the team run itself cannot be recorded, or no seat
   launched, the approval pass is restored (`approval_restored`, with
   `approval_restore_error` if the restore itself failed).

Startup guidance reaches the seat as its first prompt. Skills are delivered by
`harness_config` bundles (render and governed apply); booting a team writes no
harness config file.

## An agent reads its own seat

`coordination_manage(action="team_seat")` returns the caller's seats, teams,
teammates, incoming and outgoing edges and latest team run. A verified caller
cannot read another label's seat. `profile_status` adds a compact
`team_seats` list when the caller holds a seat.

## Control Center

Agents → Teams (`/ui/agents/teams`) lists committed specs. Each one shows its plan text,
its refusals and a Boot button that is enabled only when the plan is ready.
Boot posts the plan hash it displayed and reports a pending approval or the
per-seat outcome.
