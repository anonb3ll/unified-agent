#!/usr/bin/env bash
# Create the GCS bucket for offsite memory.db backup copies.
#
# Bucket naming (CONTRACT): ua-memory-backups-<project-short>
# Object prefix: memory-db/  → set UA_BACKUP_GCS_URI=gs://<bucket>/memory-db
#
# Local backups remain source of truth under UA_BACKUP_DIR; GCS is offsite only.
# Stream B owns sync-backup-to-gcs.sh and the Python backup path.
#
# Usage:
#   export PATH="$HOME/google-cloud-sdk/bin:$PATH"
#   export UA_GCP_PROJECT=ua-worker-<short>
#   # optional override:
#   # export UA_GCP_BACKUP_BUCKET=ua-memory-backups-<short>
#   ./deploy/gcp/create-backup-bucket.sh [--dry-run] [--project=ID] \
#       [--bucket=NAME] [--location=us-central1]
#
# Idempotent: if the bucket exists, prints ls and suggested UA_BACKUP_GCS_URI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
LOCATION="${UA_GCP_REGION:-us-central1}"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --project=*) UA_GCP_PROJECT="${1#*=}" ;;
    --bucket=*) UA_GCP_BACKUP_BUCKET="${1#*=}" ;;
    --location=*) LOCATION="${1#*=}" ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
  shift
done

ua_gcp_require_gcloud
PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"
BUCKET="$(ua_gcp_bucket_name)"
PREFIX="${UA_GCP_BACKUP_PREFIX:-memory-db}"
URI="gs://${BUCKET}/${PREFIX}"

echo "project=${PROJECT} bucket=${BUCKET} location=${LOCATION} uri=${URI}"

if gcloud storage buckets describe "gs://${BUCKET}" \
    --project="$PROJECT" &>/dev/null; then
  echo "bucket already exists; listing (idempotent no-op create)"
  gcloud storage ls "gs://${BUCKET}/" || true
  echo
  echo "Set on the desktop hub (service.env) when Stream B lands:"
  echo "  UA_BACKUP_GCS_URI=${URI}"
  exit 0
fi

CREATE_ARGS=(
  storage buckets create "gs://${BUCKET}"
  --project="$PROJECT"
  --location="$LOCATION"
  --uniform-bucket-level-access
)

if (( DRY_RUN == 1 )); then
  ua_gcp_dry_run gcloud "${CREATE_ARGS[@]}"
  echo "dry-run only; no bucket created"
  echo "would suggest: UA_BACKUP_GCS_URI=${URI}"
  exit 0
fi

gcloud services enable storage.googleapis.com --project="$PROJECT"
gcloud "${CREATE_ARGS[@]}"

# Soft-create prefix marker so operators can ls the expected path.
# Empty "directory" objects are optional; skip if API rejects.
gcloud storage cp - "gs://${BUCKET}/${PREFIX}/.keep" <<<'ua-memory-backups' \
  || echo "note: could not write ${PREFIX}/.keep (non-fatal)" >&2

echo
echo "created gs://${BUCKET}/"
echo "Suggested desktop env (offsite copies only; local backup still required):"
echo "  UA_BACKUP_GCS_URI=${URI}"
echo "Fill Live inventory gcs_bucket / UA_BACKUP_GCS_URI in CONTRACT.md (non-secret)."
