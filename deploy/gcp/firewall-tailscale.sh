#!/usr/bin/env bash
# Firewall rule: allow Tailscale UDP 41641 to tagged instances.
#
# No public application ports. MCP hub stays on the desktop; this VM is an
# additive private worker only (see deploy/gcp/CONTRACT.md).
#
# Usage:
#   export PATH="$HOME/google-cloud-sdk/bin:$PATH"
#   export UA_GCP_PROJECT=ua-worker-<short>
#   ./deploy/gcp/firewall-tailscale.sh [--dry-run] [--project=ID] [--network=default]
#
# Idempotent: if the rule exists, describes it and exits 0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
NETWORK="${UA_GCP_NETWORK:-default}"
RULE_NAME="${UA_GCP_FIREWALL_RULE:-ua-ts-allow-tailscale}"
TARGET_TAGS="ua-worker,tailscale"

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --project=*) UA_GCP_PROJECT="${1#*=}" ;;
    --network=*) NETWORK="${1#*=}" ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
  shift
done

ua_gcp_require_gcloud
PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"

echo "project=${PROJECT} rule=${RULE_NAME} network=${NETWORK} udp=41641 tags=${TARGET_TAGS}"

if gcloud compute firewall-rules describe "$RULE_NAME" \
    --project="$PROJECT" &>/dev/null; then
  echo "firewall rule already exists; describing (idempotent no-op create)"
  gcloud compute firewall-rules describe "$RULE_NAME" --project="$PROJECT"
  exit 0
fi

CREATE_ARGS=(
  compute firewall-rules create "$RULE_NAME"
  --project="$PROJECT"
  --network="$NETWORK"
  --direction=INGRESS
  --priority=1000
  --action=ALLOW
  --rules=udp:41641
  --source-ranges=0.0.0.0/0
  --target-tags="$TARGET_TAGS"
  --description="Tailscale DERP/direct UDP for ua-worker (task #522)"
)

if (( DRY_RUN == 1 )); then
  ua_gcp_dry_run gcloud "${CREATE_ARGS[@]}"
  echo "dry-run only; no firewall rule created"
  exit 0
fi

gcloud services enable compute.googleapis.com --project="$PROJECT"
gcloud "${CREATE_ARGS[@]}"

echo
echo "created ${RULE_NAME}. Ensure the VM has network tags: ua-worker,tailscale"
echo "Do not open public app/MCP ports. Hub remains on desktop 127.0.0.1:8765."
