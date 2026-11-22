"""Systemd / launchd entry point for the scheduled complete state-database backup.

``create_backup`` may include a best-effort ``gcs_upload`` field when
``UA_BACKUP_GCS_URI`` is set and this runner opts into offsite copy. Local
success is always exit 0; GCS status is emitted in the JSON only (never fails
the unit solely for offsite copy). ``UA_BACKUP_S3_URI`` adds the same
best-effort ``s3_upload`` to an S3-compatible store such as Cloudflare R2
(task #2585).

Before snapshotting, disposable telemetry is pruned so the backup — and the
live DB — stay bounded (task #919): ``agent_perf_samples`` rows past
``UA_PERF_SAMPLE_RETENTION_DAYS`` (default 14). A prune failure never blocks the
backup; the removed count is reported in the JSON as ``perf_samples_pruned``.
"""

from __future__ import annotations

import json

from . import storage
from .backup import create_backup


def main() -> int:
    try:
        perf_pruned = storage.prune_perf_samples()
    except Exception as exc:  # never let housekeeping block the backup
        perf_pruned = f"error: {exc}"
    result = create_backup(upload_to_gcs=True, upload_to_s3=True)
    result["perf_samples_pruned"] = perf_pruned
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
