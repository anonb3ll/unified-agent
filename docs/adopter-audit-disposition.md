# Adopter audit disposition

Audit baseline: main `6237a8eda0699e0dd289a21dc15194a11e7b2912` (2026-10-02).
This is a dated disposition, not the live task queue. The workspace-authority
proposal was superseded by the merged decision recorded in A09.
Integration baseline: main `40fb71952ddf9e2c371e5d1d7e3de42be5e47b00`, including
merged PR #971–#979. Implementation: task #1689, claimed by `agent-server-agent2`,
isolated product worktree.
This document accounts for every remaining finding in the adopter audit. A
proposed configuration surface is not authority to change an installation.

The report is preserved separately in the owner's Library as Markdown, offline
HTML and an evidence bundle. Its source references pin the baseline SHA; the
implementation below is a bounded remediation, not a claim that all adoption
work or current unmerged PRs are complete.

## What this change fixes

Settings preflight rejects invalid transport, TCP port, IANA timezone, unsafe
project entries and unexpanded shell variables in workspace/dev/vault paths.
Typed and `[env]` values share the checks; existing project-registry validation
is reused. Invalid overrides cannot displace a good typed value, writing
invalid settings leaves the original file unchanged, and environment priority
is preserved. Unknown `UA_*` compatibility variables remain explicitly allowed;
a complete registry and cross-field posture contract are separate follow-ups.

Built wheels include the Control Center and capability catalog inside the Python
package. An sdist carries the prepared assets and can rebuild a wheel without
Node. The shared static-resource root works from a source checkout or installed
package; the current `UA_WEB_DIST_DIR` override remains supported. Missing
release assets fail the package build instead of producing a broken wheel.

Database snapshots are private before SQLite opens their unpredictable temp
files, including in an existing readable directory. Directories writable by
other users are refused. Stale predictable temp files/symlinks are not opened or
removed. Existing snapshot modes and credential/recovery custody remain owner
responsibilities.

Work-product root/subcommand help and argument errors do not initialize or
migrate storage. Operational local commands still initialize their state after
parsing; remote submit/release behavior is unchanged. The broader CLI lifecycle,
JSON/errors and pagination contract remains a separate design project.

See [package build and artifact checks](adopter-package-build.md) and
[enrollment/recovery boundaries](adopter-recovery.md).

## Complete disposition matrix

“Fixed here” means source and scoped regression coverage in this change, not
merged/deployed. “Merged repair” identifies upstream source now integrated, not
an assertion that a deployed process runs it or that this PR has exact-head CI. “Decision needed” is an authority
or product contract this author cannot invent. “Follow-up” is explicit remaining
work; existing task anchors are reused rather than duplicated.

| ID | Audit finding / impact | Disposition | Next step or evidence |
|---|---|---|---|
| A01 | Compiled profile defaults/ceilings and privileged coordinator labels prevent a neutral authority setup. | Decision needed | Define a reviewed identity/profile/kind/locality schema and recovery principal. Reuse governance tracks #1452/#1457; unknown workers stay least-privileged. |
| A02 | Native named dispatch expects the reference fleet rather than generic identity/backend bindings. | Follow-up | Use existing explicit executor/node paths; complete neutral runtime registry in fleet track #1319 and starter-org #1706. No configurable safety bypass. |
| A03 | GitHub poller, task-space worker and Slack routing attribute work to fixed reference labels. | Follow-up | Registered service identity and backend binding must match the actual bearer. #1688/#1704/#1706 remain infrastructure/configuration anchors; repo URLs alone do not solve it. |
| A04 | Fresh operator cannot use `config_revision_manage`; Operating pane drafts are not policy commits. | Decision needed; documented here | Owner-mediated bootstrap/recovery and narrow verified administration path, with separation of requester/approver. Do not grant a cloud principal unrelated CoS powers. |
| A05 | Prefix-based workspace locality lets unbound custom labels inherit the shared workspace. | Decision needed + follow-up | Explicit reviewed locality/workspace binding; missing remote binding means DENY. Preview legacy mappings before migration, never broaden roots automatically. |
| A06 | `settings check` accepts semantic errors (transport/port/timezone/project path/unknown variable). | Focused semantics fixed here; schema follow-up | Shared typed/env checks for transport, TCP port, IANA zone, project-registry validation and unresolved shell variables; strict write/check and override-preservation tests. Unknown UA_* registry and cross-field posture validation remain open. No active settings claimant after #978/#979 merged. |
| A07 | Trusted TOML `[env]` permits some posture controls while selected others are forbidden; trust contract is unclear. | Decision needed + follow-up | Classify owner-only posture, provenance, required files and restart rules; preserve env precedence and secret refusals. Reuse runtime validators for a shared strict schema. |
| A08 | Installed wheel lacks UI and catalog; source-based Install Gate hides the gap. | Fixed here; #1689 | Package resource hook + source/installed lookup regressions + actual wheel HTTP/auth/catalog checker. Installed runtime does not need checkout/Node. |
| A09 | Service workspace precedence needs an explicit authority contract. | Strict pinning merged in #991; earlier precedence docs/tests in #984 | After init, `service.env` pins `UA_WORKSPACE`; `settings.toml` cannot move the sandbox root. #991 makes the init pin unconditional and adds the doctor diagnostic (#1825). The earlier permissive removal proposal is superseded; no service edits are proposed here. |
| A10 | Backup/evidence units assume a source venv; macOS setup relies on private fleet plists. | Follow-up | Neutral service generation from installed executable/state paths, OS-specific acceptance. #1715/#1691 are existing portability/export anchors. This PR does not repackage private fleet units. |
| A11 | Docker logs a long-lived worker bearer and omits separate operator enrollment output. | Decision needed + follow-up | Owner-only first-operator delivery/re-enrollment flow; no secrets in logs. Docker source-only credential handoff is guarded deploy scope and is not changed here. |
| A12 | Init has hostname/forced-init preservation and worker-alias edge cases. | Merged repair #978 | Tasks #1774/#1661/#1760; upstream now preserves deployment settings and rejects worker-to-human aliasing. These changes are integrated; no duplicate bootstrap/config/service.env edits. |
| A13 | Backup into existing 0755 destination under ordinary umask creates 0644 snapshots. | Fixed here | Unpredictable `mkstemp` file is 0600 before SQLite writes; three umasks/new+existing dirs, stale symlink, verification-failure cleanup and existing backup tests. Mode is an invariant, not a knob. |
| A14 | DB-only backup omits credentials, secret keys, settings, external policy/org and supervisor definitions. | Documented here; follow-up | Recovery manifest/custody checklist in adopter-recovery.md. Automated complete recovery bundle needs explicit secret export/custody authorization; DB snapshot is not installation recovery. |
| A15 | Automatic migrations can make code-only rollback impossible. | Documented here; follow-up | Preserve matched artifact/database/config set; scratch-only rehearsal and post-snapshot evidence retention decision. Existing newer-schema refusal stays enforced. |
| A16 | Background provider order/remote egress are implicit and a configured key can enable a paid leg for scheduled data. | Decision needed + follow-up | Reviewed background provider order, data permission, budgets/schedules. Reuse provider/broker #1326 and memory track #1453; no provider calls or default changes here. |
| A17 | Operator aliases are one human; several colleagues need independent verified principals and roles. | Decision needed; documented here | Dedicated principal/IdP role model preserving requester/approver distinction; don't reinterpret aliases as team members. Governance/verified-human tracks #1190/#1452. |
| A18 | Nullable org stamping is not general multi-tenancy; WorkOS row/admission organization pins may differ. | Existing work + decision needed | Dedicated hub/state per unrelated client today. Merged PR #974 fixes memory reads only; #1793/#1795/#1796 cover approval, migration and receipt boundaries. Require all-object isolation design/negative tests before shared tenants. |
| A19 | Hooks contain absolute personal paths. | Merged repair #976 | Task #1646; relative hook/tool lookup repair is integrated; no hooks/instruction edits here. |
| A20 | AGENTS CI prose contradicts merged batch CI #969. | Merged repair #971 | Task #1787; corrected batch-CI instructions are integrated; merged workflow is authoritative. This change does not touch instructions. |
| A21 | Current unlabelled PRs have no full CI; mirror skips and manual dependency audits do not prove release readiness. | Existing work + follow-up | #969 is merged; use exact-head batch/full-verification per docs/ci-batch-verify.md. #1690/#1691 own independent release gates and public artifact acceptance. No CI dispatch/labels added here. |
| A22 | Declared Python 3.11/platform bounds and resolved dependency footprint need independent artifact coverage. | Follow-up | Publish tested constraints and OS/Python matrix before selecting coherent extras; prove absent-dependency import/doctor behavior and offline model acquisition separately. No arbitrary per-library settings. |
| A23 | Public/proprietary license, product name and repository/publication destination remain owner decisions. | Decision needed | Reuse #1693/#1696. No public copy, npm/image release, license substitution, repo creation or history rewrite. |
| A24 | Checkout SHA/healthz/distribution version do not attest loaded process/build assets. | Merged repair #977 + follow-up | #1771/#1783 drift/run-record work. Extend authenticated build/artifact identity after its integrated doctor/runtime repair; no runtime restart/inspection change here. |
| A25 | Evidence export resumes one configured window after outage, not every missed date. | Documented here; follow-up | Coverage cursor/bounded backfill or explicit owner backfill procedure; keep default export disabled until destination/signing/custody chosen. |
| A26 | Artifact/DB/telemetry/ledger retention semantics differ. | Documented here; follow-up | Separate policies and explicit ledger pruning authorization. Existing signed pairs, matching windows and scoped destination deletes are preserved. |
| A27 | External SDK depends on sibling `file:` packages/private built dist. | Follow-up in canonical SDK | SDK #35 compatibility is already mirrored correctly. Release a versioned protocol/client pair and test an unrelated consumer; never patch the in-repo mirror by hand. |
| A28 | SDK demo's original-human blacklist misses a custom human. | Follow-up | Verified actor kind/minimum capabilities and conservative rejection of ambiguous identity; do not turn the blacklist into a setting. Hub approval gates remain enforced. |
| A29 | HTTP SDK lacks MCP-equivalent request timeout/cancellation. | Follow-up in canonical SDK | Request bound/cancellation and optional credential-provider API; prohibit blind mutation retry. Separate from the current wheel fix. |
| A30 | Reference personas, TUI/sign-in labels and onboarding affordances are residual fleet presentation. | Existing UI work + follow-up | Prefer configured identities/opt-in demo preset and readiness view. V2 navigation/Inbox/Runs tasks #1649–#1653 already own UI direction; do not duplicate them or make every color/emoji a setting. |
| A31 | Systemd prose is not universal macOS/Windows setup; scaffold stdio and distro hub-client states differ. | Documented boundary; follow-up | Separate OS/state-model guides; neutral launchd delivery and native Windows evidence remain open. Legacy scaffold is an isolated local server, distro is a hub client. |
| A32 | Distro scope protection assumes literal ~/dev instead of declared installation scope. | Follow-up under scaffolding | #952 is merged and its existing anti-shadowing protection stays. Make future declared-scope detection preserve hub registration safety; do not silently weaken it. |
| A33 | Public export must still work after private artifacts are deliberately excluded. | Existing work + follow-up | #946 privacy and #949 hardening are merged; #1691 owns independent exported-tree install acceptance. Private fleet files' existence is not itself a fresh runtime defect. |
| A34 | New observed task provisioning picked the cockpit when product task lacked [code-task]. | Binding recovered here; usability follow-up | Supported end-own-run/reclaim preserved all trees and structured metadata now pins absolute product path/branch/SHA. Preflight/task-create UX should reject or clarify ambiguous code-bearing tasks; no system repair expansion here. |
| A35 | Work-product --help attempted storage initialization/migration, blocking safe discovery. | Fixed here | Root/help and seven local/remote parser help paths plus argument errors use a storage-init trap; existing operational work-product tests retain coverage. |
| A36 | Broader lifecycle/JSON/error/pagination contract across 32 CLI entrypoints is incomplete. | Decision needed + follow-up | A separately scoped CLI design project. The help correction does not claim CLI-wide consistency. |

The authority/runtime/locality rows require **configuration extraction plus a
reviewed authority model**, not an unbounded environment-variable escape hatch.
Names, paths, bindings and integration destinations are installation data;
authentication, ceilings, exact-payload approval, tenant fencing, private backup
modes and refusal of unsafe paths remain security invariants.

## Recent work reconciled

Merged before the audit: PR #925 (operator settings/public-readiness foundation),
#946/#949 (privacy/export hardening), canonical SDK #35 (operator-only vocabulary
compatibility), #954 (neutral compatibility/default extraction), #952 (distro
scaffolding), #958/#962 (operating profile core/hardening), #960 (Operating pane),
#961 (runtime consumers), #969 (GitHub batch CI), #970 (scoped vault discovery
independent of denied hub workspace). None is presented as a missing feature.

At the original scope review #971–#979 were open and owned. The final upstream
check found all nine merged into main `40fb71952ddf9e2c371e5d1d7e3de42be5e47b00`,
which is integrated here: #971 CI docs; #972 small fixes/frontend dependency
repair; #973 approvals read/polling; #974 memory tenant/evidence wording; #975
scoped vault repair; #976 hooks; #977 doctor/runtime truth; #978 bootstrap; #979
policy-authorized merge evidence. Those repaired findings are no longer listed
as unmerged work. Their narrower fixes still do not establish generic service
identity, all-object tenants, artifact identity or a complete settings schema.
A historical batch does not certify this new head or the loaded deployment.

The fresh active-claim check found no owner of settings.py after that merge;
settings validation was added with dedicated tests under the user's fix request.
Current independent scopes (ledger admission, migration tenant fences and
storage post-init hooks) remain with their claimant. No file in those scopes
is modified by this PR.

## Workspace authority decision

The earlier proposal to remove the service workspace pin is superseded.
[PR #984](https://github.com/anonb3ll/unified-agent/pull/984) documented and
tested environment-over-settings precedence. Its earlier settings-enabled
init behavior was superseded by
[PR #991](https://github.com/anonb3ll/unified-agent/pull/991), which made the
authoritative `service.env` pin unconditional during init and added the
doctor's `workspace_pin` diagnostic. These are merged source evidence, not a
new claim about a running installation.

Before an installation is initialized, settings or defaults can supply its
workspace. After init, `UA_WORKSPACE` in `service.env` owns the sandbox root.
`settings.toml` records that value but cannot move the boundary. An operator
changes it through the reviewed configuration path or an explicit
`unified-agent init --workspace PATH`; this disposition does not authorize a
change. See [workspace authority](security.md#where-ua_workspace-comes-from)
for the maintained contract and `src/unified_agent/bootstrap.py` for init.

This correction changes documentation only. It does not remove a service pin,
edit a supervisor, alter vault configuration, install or restart a service, or
resolve the other historical disposition rows.

## Verification and limits

Pre-fix regressions reproduced current-main resource/backup failures; the
implementation's acceptance is pinned to the final committed head and recorded
in the PR. The installed-artifact probe checks an actual wheel outside a source
checkout with synthetic state/bearers. Source/installed layout tests, backup
permission/cleanup tests, help storage-init traps, settings/bootstrap semantic and precedence tests,
existing UI/auth/catalog and work-product tests cover the bounded fixes. Layer-map, lint/format and repository
gates remain required.

No real database/vault operation, operator credential/config change, deployment,
merge, provider generation, public publication or manually launched CI is part
of this change. Reused dependency caches are not a true cold-machine install.
Native Windows, every supported Python version, browser rendering and a complete
team/multi-tenant proof remain untested. Final PR evidence must say explicitly
whether exact-head CI/review is present or still awaiting the deploy steward's
batch; skipped checks are not green coverage.

## Smallest next stages

1. Finish this bounded package/privacy/help PR and the existing owned repairs;
   resolve the precise service source approval and the remaining strict-schema contract.
2. Make one dedicated-client installation handable: owner enrollment/recovery,
   strict shared preflight, neutral supervisors, release manifest and independent
   wheel/exported-artifact acceptance.
3. Generalize fleet/service identity bindings, explicit locality and background
   provider policy under protected authority config; verify solo local and
   headless Linux profiles.
4. Design several human principals and supported recovery, then separately
   decide whether unrelated tenants ever share a process. Finish external SDK
   packaging and the existing V2 UI roadmap with their owners.
