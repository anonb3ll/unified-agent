#!/usr/bin/env bash
# Best-effort copy of a local memory.db snapshot to GCS.
# Local backups remain the source of truth; this script never deletes local files.
#
# Usage:
#   UA_BACKUP_GCS_URI=gs://bucket/memory-db ./sync-backup-to-gcs.sh /path/to/memory-YYYYMMDDTHHMMSSZ.db
#   ./sync-backup-to-gcs.sh /path/to/snapshot.db gs://bucket/memory-db
#
# Prefers gsutil; falls back to `gcloud storage cp`. Exit 0 on success, non-zero on failure.
set -euo pipefail

usage() {
  echo "Usage: UA_BACKUP_GCS_URI=gs://bucket/prefix $0 <local-snapshot.db> [gs://bucket/prefix]" >&2
  exit 2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
fi

LOCAL_PATH=$1
GCS_URI=${2:-${UA_BACKUP_GCS_URI:-}}

if [[ -z "$GCS_URI" ]]; then
  echo "error: set UA_BACKUP_GCS_URI or pass gs://bucket/prefix as the second argument" >&2
  exit 2
fi

if [[ "$GCS_URI" != gs://* ]]; then
  echo "error: GCS URI must start with gs:// (got: $GCS_URI)" >&2
  exit 2
fi

if [[ ! -f "$LOCAL_PATH" ]]; then
  echo "error: local snapshot not found: $LOCAL_PATH" >&2
  exit 2
fi

FILENAME=$(basename -- "$LOCAL_PATH")
DEST="${GCS_URI%/}/${FILENAME}"

if command -v gsutil >/dev/null 2>&1; then
  exec gsutil cp -- "$LOCAL_PATH" "$DEST"
elif command -v gcloud >/dev/null 2>&1; then
  exec gcloud storage cp -- "$LOCAL_PATH" "$DEST"
else
  echo "error: neither gsutil nor gcloud found on PATH" >&2
  exit 127
fi
