import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db_restore_rehearsal  # noqa: E402


def test_restore_rehearsal_proves_complete_snapshot_without_host_state(isolated_env):
    report = db_restore_rehearsal.run_rehearsal()

    assert report["ok"] is True
    assert report["summary"]["passed"] == report["summary"]["total"] == 5
    assert {check["name"] for check in report["checks"]} == {
        "backup_integrity_check_passes",
        "schema_version_matches",
        "table_set_matches",
        "row_counts_match",
        "rehearsal_uses_only_temp_ua_home",
    }
