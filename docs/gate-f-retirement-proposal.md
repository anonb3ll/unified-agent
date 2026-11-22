# Gate F retirement proposal (task #328, parent #285)

Developer proposal for the narrow Gate F scope flagged as an open question in
`docs/gate-f-soak-acceptance-packet.md` §4. **Proposal only — no code changed
in this task.** Any implementation still requires a separate, explicitly
approved follow-up per the packet's §5.

## 1. Correction to the packet's dependency count

The acceptance packet listed 14 files as "dependent" on the legacy sync/
state-verification layer. A fresh call-site inventory (2026-08-10) shows that
count conflates two genuinely different mechanisms that should be evaluated
separately:

| Mechanism | Purpose | Still needed post-single-hub? |
|---|---|---|
| `is_laptop()` + `vault_inbox_dir()` / `vault_journal_dir()` laptop branches | Route laptop writes to `00 - Inbox/Laptop`, `Logs/Laptop` so a laptop running its **own hub** didn't collide with the desktop's inbox/journal | **No** — no process has a laptop-side hub writing to the vault anymore (task #316: laptop `unified-agent.service` confirmed stopped) |
| `state_sync_dir()` / `state_mirror_name()` (hostname-scoped, e.g. `state_memory.example-desktop.md`) | Prevent two *hosts* both mirroring `memory.db`→vault snapshots under the same filename (task #172's Obsidian "(Conflicted copy)" bug) | **Yes, unchanged** — scoped by hostname, not laptop/desktop; still the correct mechanism for `sync_state_to_vault`/`state_sync_verify` as a drift detector, which the packet already said to keep |

`sync.py` and `tools/state_verify.py` only use the second mechanism
(`state_sync_dir`/`state_mirror_name`), which this proposal does **not**
touch. They should not have been counted as Gate F retirement candidates —
recommend removing them from scope entirely.

## 2. Actual `is_laptop()` call sites (7, across 6 files)

Historical inventory from 2026-08-10. `is_laptop()`, `vault_inbox_dir()`, and
`vault_journal_dir()` are gone; §7 records the re-inventory. Do not treat the
paths below as the current tree.

```
src/unified_agent/config.py        — is_laptop(), vault_inbox_dir(), vault_journal_dir() (definitions)
src/unified_agent/safety.py:133    — vault write restricted to Inbox/Laptop, Logs/Laptop, Wiki when is_laptop()
src/unified_agent/tools/doctor.py:757 — _laptop_topology_check(): asserts a laptop uses stdio transport, not HTTP hub
src/unified_agent/tools/vault.py:371  — archive_root gets /Laptop suffix when is_laptop()
src/unified_agent/tools/vault.py:368  — inbox_path defaults via vault_inbox_dir()
src/unified_agent/consolidate.py:355  — inbox target via vault_inbox_dir()
src/unified_agent/merge.py:52         — merge-failure note target via vault_inbox_dir()
src/unified_agent/sleep_pass.py:320   — sleep-pass output target via vault_inbox_dir()
```

`vault_journal_dir()` is also used by `tools/vault.py:561` for daily journal
notes, and by `tools/export.py` only in a comment.

## 3. Notable finding: a stale/contradictory check

`doctor.py:_laptop_topology_check()` (the `laptop_topology` capability in
`system_doctor`) asserts a laptop deployment "must use local stdio transport,
not an HTTP hub" — this now **contradicts the shipped architecture**, where
all four `Laptop_*` identities are HTTP-hub remote clients by design
(#284/#285's entire point). It currently reads `DORMANT` only because it's
gated on `is_laptop()` being true on the *desktop* process evaluating it —
since no laptop process runs `UA_LOCATION=laptop` anymore (its local hub is
stopped), this check has had no live evaluator since the laptop hub
decommission and its assertion is now factually wrong for this program's own
target state. Recommend deleting this check as part of Gate F rather than
keeping it — it's not a "keep as-is" case like `state_sync_verify`.

## 4. Proposed narrow scope

**Remove:**
- `config.is_laptop()`, `config.vault_inbox_dir()`, `config.vault_journal_dir()`
- `safety.py:133`'s laptop-scoped write restriction (folds into: vault writes
  just use `UA_VAULT_WRITE_DIRS` as configured, no laptop special-case)
- `tools/vault.py:371`'s `/Laptop` archive-root suffix
- `tools/doctor.py`'s `_laptop_topology_check` / `laptop_topology` capability
- The three inbox-target call sites in `consolidate.py`, `merge.py`,
  `sleep_pass.py` collapse to always using the plain `"00 - Inbox"` /
  `"Logs"` literals (or a renamed non-branching helper, if a call-site
  abstraction is still wanted for readability)

**Keep, unchanged:**
- `sync.py`, `tools/state_verify.py`, `state_sync_dir()`, `state_mirror_name()`
  — hostname-scoped drift detection remains correct and useful with one
  writer

## 5. Preconditions to verify before any deletion (not yet done)

1. Confirm no process anywhere still sets `UA_LOCATION=laptop` — grep each
   laptop's env/service files (requires a laptop-side session; cannot verify
   from the desktop).
2. Confirm no vault note currently lives under `00 - Inbox/Laptop`,
   `Logs/Laptop`, or `04 - Archive/inbox-archive/Laptop` that a removal of
   the write-restriction would silently start treating differently (existing
   notes aren't moved by this change, but new laptop-labeled writes would
   stop being separated — worth a decision, not just a default).
3. Run this soak's closing evidence first — Gate F is explicitly gated on Gate
   E's soak window completing per the packet, and this proposal should not
   be actioned before 2026-08-17 regardless of scope agreement.

## 6. What this task does NOT do

No code deleted, no tests changed, no config changed. This is evidence for
Hermes/the operator's Gate F scope decision, per the acceptance packet's §5 item 2
("Any Gate F code change ... needs an explicit developer proposal and
the operator's review before any code changes").

## 7. Re-inventory, 2026-08-16: §4's removal scope has already shipped

**Sections 2–4 above describe a tree that no longer exists.** The inventory was
taken on 2026-08-10; `c5368f0` ("fix(config): retire per-host laptop vault
namespacing (Gate F, task #352)", PR #158) then removed the whole of §4's
**Remove** list in one commit. Re-run against the current checkout, every entry
is gone:

| §4 removal item | State on 2026-08-16 |
|---|---|
| `config.is_laptop()` | Absent. The only `is_laptop` in `src/` is an unrelated local variable in `webui/api.py` (`queue_health_payload`, a task-lane flag). |
| `config.vault_inbox_dir()`, `config.vault_journal_dir()` | Absent — no definitions, no call sites. |
| `safety.py:133` laptop-scoped write restriction | Absent. That line is now the hook-execution-directory guard. |
| `tools/vault.py:371` `/Laptop` archive-root suffix | Absent. `archive_root` defaults to plain `04 - Archive/inbox-archive`, `inbox_path` to plain `00 - Inbox`. |
| `tools/doctor.py:_laptop_topology_check` / `laptop_topology` | Absent — the stale check §3 recommended deleting is deleted. |
| `consolidate.py` / `merge.py` / `sleep_pass.py` inbox targets | All three use the plain `00 - Inbox/…` literal, exactly as §4 proposed. |

§4's **Keep** list is intact and untouched: `sync.py`, `tools/state_verify.py`,
`config.state_sync_dir()`, `config.state_mirror_name()`.

**Do not read this as "delete the remaining `Logs/Laptop` references."** The
matches still in `tools/vault.py` (`_read_entries`, the briefing-digest merge)
and `tools/export.py` are *readers* of historical laptop journals, not the
laptop write-routing this proposal scoped. §4 never proposed removing them, and
removing them would break `vault_briefing_digest`.

### What is actually left to decide

The scope question is settled by the code. What remains is §5 item 2, which the
shipped commit did not answer and which no code change decides: **existing notes
under `00 - Inbox/Laptop`, `Logs/Laptop`, and `04 - Archive/inbox-archive/Laptop`
are still in place and are no longer written to.** Leave them, migrate them, or
archive them — a decision, not a default.

§5 item 3's date bar (`should not be actioned before 2026-08-17`) applied to
deleting code. It does not gate a documentation reconciliation, and the code it
guarded landed before the bar regardless.

*This section re-runs the inventory and records what it found. It decides
nothing; per §6 and the acceptance packet's §5 item 2, the scope decision is
the operator's.*
