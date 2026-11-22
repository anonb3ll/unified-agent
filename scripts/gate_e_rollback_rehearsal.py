#!/usr/bin/env python3
"""Gate E rollback rehearsal (task #285, single-Tailscale-hub program).

Proves the Gate C migration mechanism (export_migration_manifest /
import_migration_manifest / rollback_migration_import, task #302) end-to-end
against realistic fixture data, as the "rollback proof" evidence Gate E
requires before any real cross-host cutover. This is a REHEARSAL: every
database it touches lives under a throwaway tempfile.TemporaryDirectory via
UA_HOME, exactly like scripts/smoke.py's isolation pattern — it never reads
or writes the real ~/.unified_agent/memory.db on this or any other host, and
performs zero network calls, zero token/service/client changes. It answers
one question only: "if we exported real state and imported+rolled-back a
manifest shaped like it, would the mechanism behave correctly?" — not "is it
safe to cut over now" (that also needs a live soak window, which this script
does not attempt).

Scenario seeded, chosen to exercise every held/inserted/rollback branch the
mechanism has:
  - two parent tasks, each with one child (parent/child relationship survives)
  - one task mid-claim (status=doing, a claimed-by note) survives verbatim
  - three tagged memory notes
  - one task on the TARGET side that already exists with a conflicting edit
    before import runs, to prove the importer holds it rather than
    overwriting local state
  - a same-manifest re-import, to prove idempotency (zero additional writes)
  - a full rollback, to prove the target is restored to its exact
    pre-import state — including that the pre-existing conflicting task
    (never touched by the import) is still there afterward

Usage: uv run python scripts/gate_e_rollback_rehearsal.py [--out PATH]
Exit codes: 0 = every check passed; 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Never let this rehearsal write real Logs/state_*.md mirrors or spawn a
# background sync thread whose target directory disappears the moment the
# tempdir context below exits.
os.environ["UA_STATE_SYNC"] = "0"


def _set_home(tmp_dir: Path, name: str) -> None:
    home = tmp_dir / name
    os.environ["UA_HOME"] = str(home)

    from unified_agent import post_init

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks


def _check(checks: list[dict], name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run_rehearsal() -> dict:
    checks: list[dict] = []
    prior_home = os.environ.get("UA_HOME")

    try:
        with tempfile.TemporaryDirectory(prefix="gate-e-rehearsal-") as tmp:
            tmp_dir = Path(tmp)

            # --- Source side: seed realistic fixture state ---
            _set_home(tmp_dir, "source_home")
            from unified_agent.tools.export import (
                export_migration_manifest,
                export_tasks,
                import_migration_manifest,
                rollback_migration_import,
            )
            from unified_agent.tools.memory import memory_save
            from unified_agent.tools.tasks import task_add, task_update

            parent_a_title = "parent A"
            parent_a = task_add(parent_a_title)
            child_a = task_add("child of A", parent_id=parent_a["id"])
            parent_b = task_add("parent B")
            child_b = task_add("child of B", parent_id=parent_b["id"])
            claimed = task_add("mid-claim task")
            task_update(
                claimed["id"],
                notes="claimed-by: Desktop_Agent1",
                status="doing",
                worktree=False,
            )
            notes = [
                memory_save(
                    f"rehearsal note {i}", title=f"Note {i}", tags=["rehearsal"], author="A"
                )
                for i in range(3)
            ]
            manifest = export_migration_manifest(write_to_vault=False)["manifest"]
            _check(
                checks,
                "source_manifest_has_expected_rows",
                len(manifest["tasks"]) == 5 and len(manifest["memory"]) == 3,
                f"tasks={len(manifest['tasks'])} memory={len(manifest['memory'])}",
            )

            # --- Target side: pre-existing conflicting row + native data ---
            _set_home(tmp_dir, "target_home")
            assert export_tasks(write_to_vault=False)["rows"] == 0  # sanity: truly disjoint
            conflicting_local = task_add(parent_a_title)
            _check(
                checks,
                "target_conflict_fixture_shares_id_with_source",
                conflicting_local["id"] == parent_a["id"],
                f"target={conflicting_local['id']} source={parent_a['id']}",
            )
            task_update(conflicting_local["id"], notes="claimed-by: Laptop_Agent1", worktree=False)
            pre_import_conflicting_row = next(
                r
                for r in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
                if r["id"] == conflicting_local["id"]
            )

            # --- Dry run: preview must not write anything ---
            preview = import_migration_manifest(manifest, apply=False)
            _check(checks, "dry_run_does_not_apply", preview["applied"] is False)
            _check(
                checks,
                "dry_run_writes_nothing",
                export_tasks(write_to_vault=False)["rows"] == 1,  # only the pre-seeded conflict
            )

            # --- Real import ---
            applied = import_migration_manifest(manifest, apply=True)
            _check(checks, "import_applied", applied["applied"] is True)
            _check(
                checks,
                "import_holds_the_conflicting_row",
                [h["id"] for h in applied["tasks"]["held"]] == [conflicting_local["id"]],
                str(applied["tasks"]["held"]),
            )
            expected_inserted = {child_a["id"], parent_b["id"], child_b["id"], claimed["id"]}
            _check(
                checks,
                "import_inserts_every_non_conflicting_task",
                set(applied["tasks"]["inserted"]) == expected_inserted,
                f"got={applied['tasks']['inserted']} expected={sorted(expected_inserted)}",
            )
            _check(
                checks,
                "import_inserts_every_memory_note",
                set(applied["memory"]["inserted"]) == {n["id"] for n in notes},
            )

            post_import = {
                r["id"]: r
                for r in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
            }
            _check(
                checks,
                "parent_child_relationship_survives_import",
                post_import[child_b["id"]]["parent_id"] == parent_b["id"],
            )
            _check(
                checks,
                "claim_history_survives_import_verbatim",
                "claimed-by: Desktop_Agent1" in post_import[claimed["id"]]["notes"]
                and post_import[claimed["id"]]["status"] == "doing",
            )
            _check(
                checks,
                "conflicting_row_left_byte_for_byte_untouched",
                post_import[conflicting_local["id"]] == pre_import_conflicting_row,
            )

            # --- Idempotency: re-applying the identical manifest writes nothing new ---
            before_reapply = export_migration_manifest(write_to_vault=False)["manifest"]
            reapplied = import_migration_manifest(manifest, apply=True)
            after_reapply = export_migration_manifest(write_to_vault=False)["manifest"]
            _check(checks, "reimport_inserts_nothing_new", reapplied["tasks"]["inserted"] == [])
            _check(
                checks,
                "reimport_is_a_true_no_op",
                before_reapply["fingerprint"] == after_reapply["fingerprint"],
            )

            # --- Rollback: target restored to its exact pre-import state ---
            rollback_migration_import(applied["rollback_token"])
            post_rollback = {
                r["id"]: r
                for r in export_migration_manifest(write_to_vault=False)["manifest"]["tasks"]
            }
            _check(
                checks,
                "rollback_removes_every_imported_task",
                not expected_inserted & set(post_rollback),
                f"leftover={expected_inserted & set(post_rollback)}",
            )
            _check(
                checks,
                "rollback_preserves_the_pre_existing_conflicting_row",
                post_rollback.get(conflicting_local["id"]) == pre_import_conflicting_row,
            )
            post_rollback_memory = export_migration_manifest(write_to_vault=False)["manifest"][
                "memory"
            ]
            _check(
                checks,
                "rollback_removes_every_imported_memory_note",
                post_rollback_memory == [],
                str(post_rollback_memory),
            )
    finally:
        # Restore whatever UA_HOME this process had before the rehearsal ran —
        # otherwise a caller (or a later test in the same process) inherits a
        # UA_HOME pointing at a tempdir that no longer exists.
        if prior_home is None:
            os.environ.pop("UA_HOME", None)
        else:
            os.environ["UA_HOME"] = prior_home

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "generated_at": datetime.now(UTC).isoformat(),
        "gate": "E",
        "task": 285,
        "checks": checks,
        "summary": {"total": len(checks), "passed": sum(c["ok"] for c in checks)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here too")
    args = parser.parse_args()

    report = run_rehearsal()
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
