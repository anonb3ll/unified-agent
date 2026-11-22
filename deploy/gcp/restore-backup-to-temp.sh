#!/usr/bin/env bash
# Download the newest GCS memory.db snapshot into scratch and verify it.
# Never writes UA_HOME/memory.db. Rollback for this drill is: rm -rf "$DEST".
#
# Usage:
#   ./restore-backup-to-temp.sh [/tmp/ua-restore-$$]
#   UA_RESTORE_OBJECT=memory-20260824T091502Z.db ./restore-backup-to-temp.sh /tmp/ua-restore
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/gcp/lib.sh
source "${SCRIPT_DIR}/lib.sh"

ua_gcp_require_gcloud

PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"
BUCKET="$(ua_gcp_bucket_name)"
PREFIX="${UA_GCP_BACKUP_PREFIX:-memory-db}"
DEST="${1:-/tmp/ua-restore-$$}"
OBJECT="${UA_RESTORE_OBJECT:-}"

if [[ "$DEST" == "$HOME/.unified_agent" || "$DEST" == "$HOME/.unified_agent/"* ]]; then
  echo "error: refusing dest inside ~/.unified_agent (live hub home)" >&2
  exit 2
fi

mkdir -m 700 -p "$DEST"
URI_PREFIX="gs://${BUCKET}/${PREFIX}"

if [[ -z "$OBJECT" ]]; then
  if command -v gsutil >/dev/null 2>&1; then
    LISTING="$(gsutil ls "${URI_PREFIX}/")"
  else
    LISTING="$(gcloud storage ls "${URI_PREFIX}/")"
  fi
  OBJECT="$(printf '%s\n' "$LISTING" | awk -F/ '{print $NF}' | grep -E '^memory-[0-9]{8}T[0-9]{6}Z\.db$' | sort | tail -1 || true)"
fi
if [[ -z "$OBJECT" ]]; then
  echo "error: no memory-*.db objects under ${URI_PREFIX}/" >&2
  exit 1
fi

echo "project=${PROJECT}"
echo "object=${OBJECT}"
echo "dest=${DEST}"

SRC_URI="${URI_PREFIX}/${OBJECT}"
if command -v gsutil >/dev/null 2>&1; then
  gsutil -q cp "$SRC_URI" "${DEST}/${OBJECT}"
else
  gcloud storage cp "$SRC_URI" "${DEST}/${OBJECT}"
fi

REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON="python3"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
fi
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON" - "$DEST" "$OBJECT" <<'PY'
import json
import sys
from pathlib import Path

from unified_agent.backup import RestoreDestinationError, stage_backup_snapshot

dest = Path(sys.argv[1])
src = dest / sys.argv[2]
try:
    result = stage_backup_snapshot(src, dest)
except RestoreDestinationError as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(2)
print(json.dumps(result, indent=2, default=str))
if not result.get("verification", {}).get("ok"):
    sys.exit(1)
PY
