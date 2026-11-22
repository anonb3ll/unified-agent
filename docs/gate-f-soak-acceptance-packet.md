# Gate F / soak-window acceptance packet (task #323, parent #285)

Read-only prep packet for the two remaining open gates in the single-hub program
(#284/#285): the **live soak window** (part of Gate E) and **Gate F** (legacy
sync/state-verification code retirement). No tokens, services, clients, routing,
legacy code, or migration state were touched to produce this packet.

## 1. Evidence already available (Gates A–D + rollback rehearsal)

| Gate | Status | Evidence |
|---|---|---|
| A — lifecycle/identity/ownership/workplace/provenance/attention contracts | Done | `docs/task-lifecycle-contract.md`, Q1–Q7 answered/actioned 2026-08-08; `waiting_review`/`cancelled` statuses shipped and deployed (commit `0903663`); full suite 1718 passed at deploy time. |
| B — read-only audits/backups + deterministic export/diff manifest | Done | `export_migration_manifest` / `compare_migration_manifests`, merged `a9bfd9b`, live-verified post-deploy 2026-08-08. |
| C — queue reconciliation + idempotent import with rollback | Done | `import_migration_manifest` / `rollback_migration_import`, merged `19133c5` (fix `a41edff` for a real parent/child FK-ordering bug found in final review — task #302). Live post-restart 2026-08-07. |
| D — red-tier token/service/client cutover, staged | Substantially done | All four `Laptop_*` identities (Claude/Codex/Hermes/agy) confirmed connected to the desktop hub via `system_doctor` + `agent_list` `hub_request` activity; laptop's own `unified-agent.service` confirmed stopped by the operator (#316, closed 2026-08-10). |
| E — rollback mechanism proof | Mechanism proven, soak not run | `scripts/gate_e_rollback_rehearsal.py` (PR #143) — 16/16 checks pass against fixture data under a throwaway `UA_HOME`: disjoint-manifest import, conflict hold (no overwrite), re-import idempotency, full rollback. Zero real host state touched. |

Boundary tests for the workspace fail-closed fix (task #299) are also live-verified:
all four `Laptop_*` identities independently confirmed `workspace: "denied"` with no
fallthrough to the shared desktop root.

## 2. Remaining evidence needed

### Live soak window (completes Gate E)
Not yet run. Requires real single-hub observation time with the desktop hub and
remote laptop clients — the fixture rehearsal
proves the *mechanism*, not production behavior under real traffic from all agents
over time.

### Gate F — legacy retirement review
Not started. No retirement work has been scoped or attempted; this packet's §4 is
the first inventory pass.

## 3. Proposed soak plan

**Observation period:** 24 consecutive hours of normal multi-agent usage.

Revised from 7 days (task #499). The original window was sized to cover a full weekly
cycle; at this repo's current cadence a week-long clock that any stop condition resets
to zero gates the migration on calendar time rather than on evidence, and in practice
the signal has always been legible within the first day. 24h is the top of the
standard band here precisely because this gate carries data-loss risk.

Be explicit about what the shorter window gives up: it no longer *passively* observes
the laptop sleep/wake and desktop dual-boot patterns that motivated the weekly cycle.
Those must now be exercised deliberately inside the 24h — sleep and wake each laptop
at least once, and boot the desktop into its other OS and back — rather than waited
for. A window that does not exercise them has not tested them.

If a stop condition triggers: remediate, then observe for a further 2h to confirm the
fix, rather than restarting the full clock. Restarting from zero was never what
produced confidence — the stop conditions below are, and they are evaluated
continuously.

**Signals to capture daily** (via existing `system_doctor` / `agent_list` / journal,
with no new instrumentation required):
- Hub uptime and unplanned restarts (`journalctl --user -u unified-agent`).
- One bounded authenticated connectivity probe per `Laptop_*` identity, recorded as
  pass/fail with timestamp; record `agent_list`'s `last_seen` and `last_seen_via` as
  corroborating liveness evidence. `agent_list` does not expose request counts, so
  do not report success/failure totals unless a journal query provides them.
- Any `workspace: "denied"` responses that were *not* expected (i.e., anything other
  than the four documented laptop DENY entries — a regression signal).
- Read-only parity snapshots of the memory/task DB IDs and the host-scoped vault
  mirror files — see §4. Do **not** call `state_sync_verify()` during the soak:
  its current implementation automatically calls `sync_state_now()` on drift and
  would mutate the vault. A future dry-run verifier may replace this manual check.
- Any task-queue anomalies flagged by `monitor.py`'s existing anomaly rules.
- Manual note of any session where a laptop agent fell back to native-tools-only
  (expected during a genuine hub outage, but should be rare and explainable).

**Stop conditions (halt soak, do not proceed to Gate F):**
- Any laptop identity reaches the shared desktop `UA_WORKSPACE` root (workspace
  isolation regression — the exact failure #299 was built to prevent).
- Any task/memory data loss or corruption on the desktop hub.
- More than one unplanned hub restart within the observation window, not explained
  by the known dual-boot-off condition (previously expressed as a per-48h rate; the
  window is now shorter than that period, so the count is scoped to the window).
- Any `waiting_for review`/ownership/claim-conflict detected mid-soak that the
  lifecycle contract (Gate A) does not already resolve.

**Rollback trigger:** any stop condition above → laptops fall back to their
pre-migration stdio registration per `docs/desktop-migration.md`'s Rollback section
(`mv ~/.unified_agent.pre-hub.bak ~/.unified_agent` on whichever laptop needs it,
re-add stdio MCP registration). This does not require re-provisioning tokens since
the desktop hub keeps running; it only affects which laptop client config is active.

**Evidence format:** a note at the start of the window, one per notable event, and
one at the close — appended to this packet's parent task (#285) or a dedicated
soak-log memory note tagged `project:unified-agent`, `soak-window`, `task-285`. Each
carries a signal snapshot + pass/fail against the stop conditions above, and the
closing note additionally records that the sleep/wake and dual-boot exercises were
actually performed. That closing note closes out the soak evidence requirement.

## 4. Inventory: legacy sync/state-verification paths and dependencies

**Current tree:** `config.is_laptop()`, `config.vault_inbox_dir()`, and
`config.vault_journal_dir()` were removed (Gate F, task #352).
`config.state_sync_dir()` and `config.state_mirror_name()` remain. The
re-inventory is [gate-f-retirement-proposal.md](gate-f-retirement-proposal.md) §7.
The list below is the soak-time inventory, not a map of today's call sites.

The single-hub architecture retires the *reason* the following machinery exists —
it was built so a laptop running its **own independent hub with its own
`memory.db`** could mirror state into the shared Obsidian vault without colliding
with the desktop's mirror (task #172: unscoped filenames caused Obsidian Sync
"(Conflicted copy)" duplicates). With the laptop now a remote client of one hub and
no local DB to mirror, this whole layer has no laptop-side producer left — but it is
**not a self-contained module**; it is threaded through core write paths.

**Core mechanism:**
- `src/unified_agent/sync.py` — debounced scheduler + `sync_state_to_vault()` /
  `sync_state_now()`, the DB→vault snapshot writer.
- `src/unified_agent/tools/state_verify.py` — `state_sync_verify` MCP tool; compares
  DB row IDs against vault JSON exports, auto-resyncs on drift. Includes an explicit
  legacy-filename fallback (`_mirror_path`) for pre-task-#172 unscoped mirrors.
- `src/unified_agent/config.py` — `is_laptop()`, `vault_inbox_dir()`,
  `vault_journal_dir()`, `state_sync_dir()`, `state_mirror_name()`: the per-host
  namespacing (`Logs/Laptop/...`, `state_memory.<host>.md`) that made multi-writer
  vault sync safe.

**Confirmed dependents (14 files reference this layer):**
`merge.py`, `reviewer.py`, `consolidate.py`, `storage.py`, `safety.py`, `server.py`
(tool registration), `sleep_pass.py`, `sync.py`, `sync_cli.py`, `tools/doctor.py`,
`tools/export.py`, `tools/state_verify.py`, `tools/vault.py`, `webui/api.py`.

**What this means for Gate F:** this is not a single dead code path to delete — it's
load-bearing for every merge/review/consolidate write path's vault mirroring, for
`system_doctor`, and for the `state_sync_verify` tool itself, which stays *useful*
even post-single-hub as a DB↔vault drift detector (just no longer needs the
per-host collision defense). A safe Gate F scope is likely narrower than "delete this
layer": retire the *per-host* namespacing (`is_laptop()`, `Logs/Laptop/` split,
host-scoped filenames) once there is genuinely only ever one writer, while keeping
`sync_state_to_vault`/`state_sync_verify` as-is. That distinction needs an explicit
developer proposal and the operator's review before any code changes — out of scope for
this packet.

## 5. Actions requiring the operator's explicit approval

1. Starting the 24h soak window clock (a schedule/attention commitment, not a
   code change, but affects what "done" means for #284/#285).
2. Any Gate F code change — even the narrower "retire per-host namespacing only"
   scope in §4 — since it touches `merge.py`/`reviewer.py`/`server.py` write paths.
3. Declaring the soak complete and moving #284/#285 toward closure.
4. Any future decision to fully delete `sync.py`/`state_verify.py` rather than
   narrow them (would remove the DB↔vault drift-detection safety net entirely).

## Boundaries respected in producing this packet

No tokens, services, clients, routing, or migration state were changed. No legacy
code was touched or removed. No task was closed by this packet (#285 remains open;
this file only adds evidence for review).
