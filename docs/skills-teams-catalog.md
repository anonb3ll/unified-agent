# Skills & Teams catalog

The Control Center's **Agents → Skills** and **Agents → Teams** pages
(`/ui/agents/skills`, `/ui/agents/teams?tab=registry`) are an adapter over
`skills_registry`, `org.yaml`, and the immutable org configuration revision chain.
It creates no catalog table, object kind, or separate assignment registry.

Authenticated catalog readers see every published skill version with its SHA-256,
source, publisher, publication time, and installation eligibility. Team entries
show captured responsibility, membership, decision-rights/epic/lane metadata,
org revision provenance, and historical definitions. Each office retains its
matching capture even when another office changes outside the revision chain.
The projection reads complete hash-verified history, including definitions older
than the bounded configuration summary window.
Current member profiles and
skill assignments are explicitly current bindings. A definition's optional
`skill_versions` metadata stays in its org revision and is shown separately.
Membership and scope metadata grant no additional authority.

`GET /api/skills-teams` reads the catalog. An optional `surface_dir` compares
installed reviewed package hashes against latest published versions. A drift failure is
reported for that surface while the independent catalog remains visible, so a
new destination can be installed before its directory exists. The human operator
can read metadata through existing `crew_view` status authority, with all write
controls disabled. Filesystem drift requires the registry profile authority;
`permissions.drift` controls its UI affordance. Relative drift paths use the same
active worktree or workspace as installation. Explicit configured audit roots
remain readable, and scans refuse skill links escaping the selected surface.
Clear the selected surface to recover the base catalog after a scan error.

`POST /api/action/skills-teams` accepts a JSON object with `action` and `params`:

* `publish`: `{name, path}`. Uses canonical `skill_review`, secret checks, and the
  existing immutable registry publisher. High findings block publication.
* `install`: `{name, version, content_hash, surface_dir, expected_hash?}`. Installs
  the full immutable package (SKILL.md, scripts, references, and assets), including an explicitly chosen older version,
  through `fs_write` profile, policy, workspace, protected-path, and approval
  gates. Relative paths honor the caller's active worktree, otherwise its workspace.
  Creates a missing skill directory. Existing differing bytes are refused
  unless their current SHA-256 is supplied; the lower approval binds that exact
  base hash (or absence) and the actual writer rechecks it after approval.
* `team_install`: `{team_id, revision, content_hash}`. Restores one captured office
  through the existing org revision commit seam, preserving all other offices.
  Requires chief-of-staff configuration authority and a human approval bound to
  the selected definition and the exact live org file. It does not change member
  profiles or current skill assignments.

These actions also use the existing `skill_registry` consolidated tool (catalog,
install, and team_install actions); no new tool is added. Publication uses the existing registry version model with bounded, versioned
package manifests and immutable captured payloads. Every member's path, SHA-256,
size and executable mode is attested; support-only changes mint a new version.
Canonical skill review and content audit run over the payload before publication.
Symlinks, unsafe or secret-bearing paths, missing local references, special files,
credential material, executable binary assets and review-window source drift are
refused. Legacy rows without package manifests retain their supporting-file refusal.
Installing a reviewed older version uses its stored bytes, not the mutable source.

Attested installations stage a complete directory, recheck the live base and every
incoming/removed path against the existing fs_write scope and policy, then activate
the staged tree. Approval binds the manifest, removed paths and exact prior package
hash; a differing local tree needs its observed package hash. Failed staging leaves
approval unused, failed activation restores the previous tree, and actual mutation
spends receipts even if later evidence fails. Replacing a multi-file version with a
single-file version removes old supporting files only after the same path/policy
checks. File hashes and executable modes participate in per-surface drift; files
added, removed or edited locally make the surface stale. Filesystem reach and
profiles are not widened. See [Reviewed skill packages](skill-packages.md).
Approval responses use HTTP 202 with `pending` and `approval_id`. After human
approval, submit the same payload again. A different local base or live org file
requires a new approval. Outer registry passes remain unused when a nested
installer parks, refuses, or finds the exact version already installed; actual
nested mutation spends them even if later evidence recording fails.
Successful skill installation records version/hash/path
and publisher provenance on the evidence chain; team installation appends an
ordinary org configuration revision.

## Starter teams

The same catalog includes four reviewed, public-safe starter packages:
`implementation-pair`, `adversarial-review`, `conveyor`, and `research-team`.
Each includes team/seat guidance and a role-bound workflow. Choose an existing
organization office, explicitly bind every role to a distinct registered label
and harness, and select an existing operating profile and trust preset. Each seat
must already have that preset; installation never changes profile or trust policy.
Implementation builder/checker and adversarial reviewers require distinct harnesses.

`starter_preview` and `starter_install` accept the same `params` object:

```json
{
  "template_id": "implementation-pair",
  "team_id": "example-office",
  "content_hash": "<SHA-256 from starter_templates catalog entry>",
  "operating_profile": "<existing profile>",
  "trust_preset": "active-worktree",
  "bindings": {
    "builder": {"label": "<registered builder>", "harness": "codex"},
    "checker": {"label": "<registered checker>", "harness": "claude"}
  }
}
```

Bindings may include an existing host, model and workspace specification.
The catalog form provides a preview and governed install. Installation replaces
the selected office's team spec in the policy revision chain, preserving other
policy fields. An explicit human configuration approval binds the complete
candidate policy, reviewed template hash and exact live base. Concurrent edits
refuse the install; unchanged repeats create no revision. The workflow remains
repo-reviewed code: policy stores only the template version/hash and role bindings,
not executable steps. Template upgrades require a new reviewed installation.

After installation the existing team boot panel addresses the office ID, and
`workflow_list` includes `starter-<office-id>`. Start it through `workflow_run`
with `inputs={"objective": "...", "head": "<full immutable commit SHA>"}`;
research requires only `objective`. Mission context is frozen onto every durable
role task. For implementation-pair and conveyor, `head` is the starting commit,
not the review target. Before handing off, the builder updates its current task
with `evidence_ref="commit:<full resulting SHA>"`. Missing or malformed evidence
refuses the handoff before its tool effect; the checker/reviewer task durably
receives that resulting exact review head. Adversarial review uses the input
`head` as its shared immutable review target. All use explicit role handoffs. Adversarial and research lead steps create two ordinary task rows
on the reviewers'/researchers' lanes so both workers can run concurrently. The
lead must collect both result task/artifact references before its synthesis step.
These steps create queue inventory; they do not dispatch, claim for another seat,
merge, deploy or authorize release. Explicit workflow closure reasons still apply.

**Current runtime boundary:** every configured trust preset currently refuses
`workflow_run`, task mutation/claim tools, and native execution without supported
containment. A valid template install therefore does not imply live workflow or
boot acceptance. Preview and boot retain the current trust/readiness refusals;
operator tooling displays them. Test-double boot coverage proves the integration
without launching a model or changing permissions. Live preset-bound execution
needs a separately reviewed capability/containment change; these templates add
no such authority.
