#!/usr/bin/env bash
# Bootstrap (or describe) the slice-1 Tailscale worker VM.
#
# Creates an always-free-eligible e2-micro in us-central1 (fallback zones
# documented in CONTRACT.md). Defaults to an ephemeral external IP so the
# first-boot SSH + Tailscale install path works on a fresh project without
# separate Cloud NAT / IAP setup. Pass --no-external-ip only after connectivity
# (NAT/IAP or an existing Tailscale path) is already provisioned.
#
# Naming (see deploy/gcp/CONTRACT.md):
#   instance: ua-worker
#   project:  UA_GCP_PROJECT or --project (ops fills Live inventory)
#
# Usage:
#   export PATH="$HOME/google-cloud-sdk/bin:$PATH"
#   export UA_GCP_PROJECT=ua-worker-<short>   # required unless --project
#   ./deploy/gcp/bootstrap-e2-micro.sh [--dry-run] [--no-external-ip] \
#       [--zone=us-central1-a] [--project=ID] [--disk-gb=30]
#
# Does NOT: install Tailscale auth keys, start the MCP hub, or write secrets.
# Idempotent: if the instance already exists, prints describe and exits 0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
WITH_EXTERNAL_IP=1
ZONE="${UA_GCP_ZONE:-us-central1-a}"
DISK_GB=30
INSTANCE_NAME="${UA_GCP_INSTANCE:-ua-worker}"
MACHINE_TYPE="e2-micro"
IMAGE_FAMILY="debian-12"
IMAGE_PROJECT="debian-cloud"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --with-external-ip) WITH_EXTERNAL_IP=1 ;;
    --no-external-ip) WITH_EXTERNAL_IP=0 ;;
    --zone=*) ZONE="${1#*=}" ;;
    --project=*) UA_GCP_PROJECT="${1#*=}" ;;
    --disk-gb=*) DISK_GB="${1#*=}" ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
  shift
done

ua_gcp_require_gcloud
PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"

if ! [[ "$DISK_GB" =~ ^[0-9]+$ ]] || (( DISK_GB < 10 || DISK_GB > 30 )); then
  echo "error: --disk-gb must be 10–30 (always-free STANDARD PD cap is 30 GB)" >&2
  exit 1
fi

echo "project=${PROJECT} instance=${INSTANCE_NAME} zone=${ZONE} machine=${MACHINE_TYPE} disk=${DISK_GB}GB external_ip=${WITH_EXTERNAL_IP}"

if gcloud compute instances describe "$INSTANCE_NAME" \
    --project="$PROJECT" --zone="$ZONE" &>/dev/null; then
  echo "instance already exists; describing (idempotent no-op create)"
  gcloud compute instances describe "$INSTANCE_NAME" \
    --project="$PROJECT" --zone="$ZONE" \
    --format='yaml(name,status,machineType,networkInterfaces,disks)'
  exit 0
fi

CREATE_ARGS=(
  compute instances create "$INSTANCE_NAME"
  --project="$PROJECT"
  --zone="$ZONE"
  --machine-type="$MACHINE_TYPE"
  --image-family="$IMAGE_FAMILY"
  --image-project="$IMAGE_PROJECT"
  --boot-disk-size="${DISK_GB}GB"
  --boot-disk-type=pd-standard
  --tags=ua-worker,tailscale
  --metadata=enable-oslogin=TRUE
)

if (( WITH_EXTERNAL_IP == 0 )); then
  CREATE_ARGS+=(--network-interface=no-address)
  echo "warning: creating without external IP — requires Cloud NAT and an SSH path (IAP or Tailscale) before bootstrap can continue" >&2
else
  echo "warning: creating with ephemeral external IP — restrict SSH and prefer Tailscale-only access after join" >&2
fi

if (( DRY_RUN == 1 )); then
  ua_gcp_dry_run gcloud "${CREATE_ARGS[@]}"
  echo "dry-run only; no instance created"
  exit 0
fi

# Ensure Compute API is available (idempotent enable).
gcloud services enable compute.googleapis.com --project="$PROJECT"

gcloud "${CREATE_ARGS[@]}"

echo
echo "created ${INSTANCE_NAME}. Next:"
echo "  1. ./deploy/gcp/firewall-tailscale.sh --project=${PROJECT}"
echo "  2. Join Tailscale on the VM (auth key via operator channel — never git)"
echo "  3. Fill Live inventory in CONTRACT.md / ops-state.md (non-secret)"
echo "  4. Prefer dropping external IP once Tailscale works"
