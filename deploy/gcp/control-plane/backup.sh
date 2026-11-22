#!/usr/bin/env bash
# Scheduled backup on the control-plane VM (task #2148). Runs as root from
# ua-hub-backup.service; installed to /usr/local/lib/unified-agent/backup.sh.
#
# 1. The hub image's own backup runner writes a verified memory.db snapshot to
#    /data/backups inside the container (= /mnt/ua-data/backups on the disk).
#    It runs with the hub's environment through the entrypoint passthrough,
#    exactly like `railway ssh` + entrypoint on Railway.
# 2. The newest snapshot is copied to the fleet GCS backup bucket with
#    deploy/gcp/sync-backup-to-gcs.sh, under a per-host prefix so it never
#    mixes with the Mac hub's objects. The VM's service account authenticates
#    (bucket-scoped objectCreator/objectViewer); no key file is involved.
#
# UA_CP_BACKUP_GCS_URI comes from /etc/unified-agent/backup.env. Unset means
# local-only. A GCS failure exits non-zero so the unit shows failed, but the
# local snapshot is already on the disk (and in the next disk snapshot).
set -euo pipefail

LIB_DIR=/usr/local/lib/unified-agent
BACKUP_DIR=/mnt/ua-data/backups
CONTAINER=ua-hub

if [[ -r /etc/unified-agent/backup.env ]]; then
  # shellcheck source=/dev/null
  . /etc/unified-agent/backup.env
fi

docker exec "$CONTAINER" /app/deploy/docker/entrypoint.sh \
  python -m unified_agent.backup_runner

newest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'memory-*.db' -printf '%f\n' \
  | sort | tail -n 1)"
if [[ -z "$newest" ]]; then
  echo "backup: no memory-*.db snapshot in $BACKUP_DIR" >&2
  exit 1
fi

if [[ -z "${UA_CP_BACKUP_GCS_URI:-}" ]]; then
  echo "backup: local snapshot $newest kept; UA_CP_BACKUP_GCS_URI unset (local-only)"
  exit 0
fi

"$LIB_DIR/sync-backup-to-gcs.sh" "$BACKUP_DIR/$newest" "$UA_CP_BACKUP_GCS_URI"
echo "backup: $newest copied to $UA_CP_BACKUP_GCS_URI/"
