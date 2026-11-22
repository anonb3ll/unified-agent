#!/usr/bin/env bash
# Stand up the unified-agent cloud control plane on a GCP VM (task #2148).
#
# One run creates (or finds) every resource and provisions the host:
#   1. daily snapshot schedule        resource policy <instance>-daily
#   2. data disk                      <instance>-data, pd-standard, separate from boot
#   3. firewall                       reuses the tag-scoped Tailscale UDP and IAP SSH
#                                     rules; no public app or MCP port is opened
#   4. VM                             e2-medium Debian 12, no external IP, beside
#                                     ua-worker in us-central1
#   5. provision over IAP SSH         ships the pinned source (git archive) or an
#                                     image ref, the env file and the tailnet key,
#                                     then runs install.sh on the VM
#   6. verify over IAP SSH            verify.sh --restart on the VM
#
# DRY-RUN BY DEFAULT: prints every gcloud/ssh command and changes nothing.
# Only --apply creates resources or touches the VM. Read-only describe calls
# run in both modes so the plan shows what already exists.
#
# Usage:
#   UA_GCP_PROJECT=<project> deploy/gcp/control-plane/bootstrap.sh [--apply]
#       [--ref=<git ref> | --image=<image ref>]
#       [--env-file=PATH] [--tailscale-authkey-file=PATH]
#       [--zone=us-central1-a] [--instance=ua-control-plane]
#       [--machine-type=e2-medium] [--data-disk-gb=64] [--boot-disk-gb=30]
#       [--service-account=EMAIL] [--backup-gcs-uri=gs://...|none]
#       [--ssh-key-file=PATH] [--infra-only] [--skip-verify] [--init-data-disk]
#
# The data disk is formatted only on the run that creates it (install.sh gets
# --init-data-disk then), and only if every probe agrees it is blank. If that
# first run stopped before install.sh, re-run with --init-data-disk.
#
# Secrets: --env-file (filled hub.env.template) and --tailscale-authkey-file
# must be mode 0600 files outside the repo. They travel over SSH stdin, are
# never passed as arguments, and are never printed. Values in the env file
# may be Secret Manager references (see hub.env.template).
#
# SSH: IAP tunnel with a file key and `IdentityAgent none`, so it never waits
# on an interactive agent (the desktop's `gcp` alias does). The key defaults
# to gcloud's own ~/.ssh/google_compute_engine; override with --ssh-key-file.
#
# Idempotent: existing resources are described and kept; install.sh keeps an
# existing filesystem and tailnet login.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GCP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${GCP_DIR}/../.." && pwd)"
# shellcheck source=../lib.sh
source "${GCP_DIR}/lib.sh"

APPLY=0
ZONE="${UA_GCP_ZONE:-us-central1-a}"
INSTANCE="${UA_GCP_CP_INSTANCE:-ua-control-plane}"
MACHINE_TYPE=e2-medium
DATA_DISK_GB=64
BOOT_DISK_GB=30
IMAGE_FAMILY=debian-12
IMAGE_PROJECT=debian-cloud
NETWORK="${UA_GCP_NETWORK:-default}"
SERVICE_ACCOUNT=""
BACKUP_GCS_URI=""
REF=""
IMAGE=""
ENV_FILE=""
TS_KEY_FILE=""
SSH_KEY_FILE="${UA_GCP_SSH_KEY_FILE:-$HOME/.ssh/google_compute_engine}"
INFRA_ONLY=0
INIT_DATA_DISK=0
SKIP_VERIFY=0
# Snapshots: daily at 03:00 UTC (after the 02:30-02:55 in-VM backup), kept 14 days,
# stored in the region, kept if the disk is ever deleted.
SNAPSHOT_START=03:00
SNAPSHOT_RETENTION_DAYS=14
NETWORK_TAGS=tailscale,ua-control-plane

usage() {
  sed -n '2,46p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --project=*) UA_GCP_PROJECT="${1#*=}" ;;
    --zone=*) ZONE="${1#*=}" ;;
    --instance=*) INSTANCE="${1#*=}" ;;
    --machine-type=*) MACHINE_TYPE="${1#*=}" ;;
    --data-disk-gb=*) DATA_DISK_GB="${1#*=}" ;;
    --boot-disk-gb=*) BOOT_DISK_GB="${1#*=}" ;;
    --service-account=*) SERVICE_ACCOUNT="${1#*=}" ;;
    --backup-gcs-uri=*) BACKUP_GCS_URI="${1#*=}" ;;
    --ref=*) REF="${1#*=}" ;;
    --image=*) IMAGE="${1#*=}" ;;
    --env-file=*) ENV_FILE="${1#*=}" ;;
    --tailscale-authkey-file=*) TS_KEY_FILE="${1#*=}" ;;
    --ssh-key-file=*) SSH_KEY_FILE="${1#*=}" ;;
    --infra-only) INFRA_ONLY=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --init-data-disk) INIT_DATA_DISK=1 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
  shift
done

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

# run CMD...: execute under --apply, print otherwise.
run() {
  if (( APPLY == 1 )); then
    printf '+ %s\n' "$*"
    "$@"
  else
    ua_gcp_dry_run "$@"
  fi
}

# --- validation (both modes) -------------------------------------------------
[[ "$ZONE" == us-central1-* ]] || die "--zone must be in us-central1 (beside ua-worker)"
REGION="${ZONE%-*}"
[[ "$INSTANCE" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || die "invalid --instance name"
case "$MACHINE_TYPE" in
  e2-micro|e2-small|f1-micro|g1-small) die "--machine-type must be e2-medium or larger" ;;
esac
if ! [[ "$DATA_DISK_GB" =~ ^[0-9]+$ ]] || (( DATA_DISK_GB < 50 || DATA_DISK_GB > 100 )); then
  die "--data-disk-gb must be 50-100 (task #2148 sizing)"
fi
if ! [[ "$BOOT_DISK_GB" =~ ^[0-9]+$ ]] || (( BOOT_DISK_GB < 20 || BOOT_DISK_GB > 50 )); then
  die "--boot-disk-gb must be 20-50"
fi
DATA_DISK="${INSTANCE}-data"
SNAPSHOT_POLICY="${INSTANCE}-daily"

if (( INFRA_ONLY == 0 )); then
  [[ -n "$REF" || -n "$IMAGE" ]] || die "pass --ref=<git ref> (pinned checkout) or --image=<ref>, or --infra-only"
  [[ -z "$REF" || -z "$IMAGE" ]] || die "pass --ref or --image, not both"
  [[ -n "$ENV_FILE" ]] || die "pass --env-file=PATH (a filled copy of hub.env.template)"
fi

# secret_file PATH LABEL: exists, regular, not group/world readable, outside the repo.
secret_file() {
  local path="$1" label="$2" mode real
  [[ -f "$path" ]] || die "$label not found: $path"
  mode="$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path")"
  [[ "$mode" =~ ^[0-7]?[0-7]00$ ]] || die "$label must be mode 0600 or tighter (is $mode): chmod 600 $path"
  real="$(cd "$(dirname "$path")" && pwd -P)/$(basename "$path")"
  [[ "$real" != "$REPO_ROOT"/* ]] || die "$label must live outside the repository"
}
if [[ -n "$ENV_FILE" ]]; then
  secret_file "$ENV_FILE" "--env-file"
  # Same pinned-key refusal install.sh applies, caught before anything runs.
  bad="$(grep -oE '^(UA_HOSTED|UA_EXECUTION_LOCALITY|UA_STATE_MOUNT_PATH|UA_HOME|UA_ALLOW_EPHEMERAL_STATE|UA_ALLOW_ROOT|UA_DOCKER_ALLOW_INSECURE|PORT|UA_HTTP_HOST|UA_HTTP_PORT|UA_TRANSPORT|UA_ENFORCE_AUTH|RAILWAY_[A-Z_]*)=.' "$ENV_FILE" \
    | cut -d= -f1 | sort -u | tr '\n' ' ' || true)"
  [[ -z "$bad" ]] || die "--env-file sets keys the unit pins: $bad"
fi
[[ -z "$TS_KEY_FILE" ]] || secret_file "$TS_KEY_FILE" "--tailscale-authkey-file"

# Every value that reaches the VM's root shell is checked against a strict
# allowlist here, before any gcloud call, and is also quoted with printf %q
# when the remote command is built. Shell syntax never gets that far.
IMAGE_RE='^[a-z0-9]+(([._-]+|[/:])[A-Za-z0-9]+)*(@sha256:[0-9a-f]{64})?$'
GCS_URI_RE='^gs://[a-z0-9][a-z0-9_.-]{1,220}[a-z0-9](/[A-Za-z0-9_][A-Za-z0-9_.-]*)*/?$'
GIT_REF_RE='^[A-Za-z0-9_][A-Za-z0-9_./^~-]*$'
if [[ -n "$IMAGE" ]] && { ! [[ "$IMAGE" =~ $IMAGE_RE ]] || (( ${#IMAGE} > 255 )); }; then
  die "--image must be a plain image reference (registry/path:tag or @sha256:<digest>)"
fi
if [[ -n "$BACKUP_GCS_URI" && "$BACKUP_GCS_URI" != none ]]; then
  [[ "$BACKUP_GCS_URI" =~ $GCS_URI_RE && "$BACKUP_GCS_URI" != *..* ]] \
    || die "--backup-gcs-uri must be gs://<bucket>/<prefix> (letters, digits, . _ -) or 'none'"
fi
VCS_REF=""
if [[ -n "$REF" ]]; then
  [[ "$REF" =~ $GIT_REF_RE && "$REF" != *..* ]] || die "--ref must be a branch, tag or commit name"
  VCS_REF="$(git -C "$REPO_ROOT" rev-parse --verify --quiet --end-of-options "${REF}^{commit}")" \
    || die "--ref '$REF' is not a commit in $REPO_ROOT (git fetch first?)"
  [[ "$VCS_REF" =~ ^[0-9a-f]{40}$ ]] || die "--ref did not resolve to a commit SHA"
fi

ua_gcp_require_gcloud
PROJECT="$(ua_gcp_project)"
ua_gcp_require_project "$PROJECT"
[[ -n "$SERVICE_ACCOUNT" ]] || SERVICE_ACCOUNT="ua-backup-uploader@${PROJECT}.iam.gserviceaccount.com"
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid project id: $PROJECT"
# The backup destination matters only to the VM-side install; --infra-only
# never resolves a bucket, so it works in any project.
if (( INFRA_ONLY == 1 )); then
  BACKUP_GCS_URI=""
elif [[ -z "$BACKUP_GCS_URI" ]]; then
  BACKUP_GCS_URI="gs://$(ua_gcp_bucket_name)/${UA_GCP_BACKUP_PREFIX}/${INSTANCE}"
  [[ "$BACKUP_GCS_URI" =~ $GCS_URI_RE ]] || die "derived backup URI is not valid; pass --backup-gcs-uri"
elif [[ "$BACKUP_GCS_URI" == none ]]; then
  BACKUP_GCS_URI=""
fi

printf 'mode=%s project=%s zone=%s instance=%s machine=%s data_disk=%sGB boot_disk=%sGB\n' \
  "$([[ $APPLY == 1 ]] && echo APPLY || echo dry-run)" "$PROJECT" "$ZONE" "$INSTANCE" \
  "$MACHINE_TYPE" "$DATA_DISK_GB" "$BOOT_DISK_GB"
printf 'artifact=%s backup=%s service_account=%s\n' \
  "${VCS_REF:+pinned checkout $VCS_REF}${IMAGE:+image $IMAGE}$( ((INFRA_ONLY == 1)) && echo '(infra only)')" \
  "$( ((INFRA_ONLY == 1)) && echo n/a || echo "${BACKUP_GCS_URI:-local-only}")" "$SERVICE_ACCOUNT"

exists() { "$@" --project="$PROJECT" >/dev/null 2>&1; }

# --- 1. snapshot schedule ------------------------------------------------------
step "snapshot schedule ${SNAPSHOT_POLICY}"
if exists gcloud compute resource-policies describe "$SNAPSHOT_POLICY" --region="$REGION"; then
  echo "exists; keeping"
else
  run gcloud compute resource-policies create snapshot-schedule "$SNAPSHOT_POLICY" \
    --project="$PROJECT" --region="$REGION" \
    --daily-schedule --start-time="$SNAPSHOT_START" \
    --max-retention-days="$SNAPSHOT_RETENTION_DAYS" \
    --on-source-disk-delete=keep-auto-snapshots \
    --storage-location="$REGION" \
    --description="Daily snapshots of the control-plane data disk (task #2148)"
fi

# --- 2. data disk ----------------------------------------------------------------
step "data disk ${DATA_DISK}"
if exists gcloud compute disks describe "$DATA_DISK" --zone="$ZONE"; then
  echo "exists; keeping (never resized or reformatted here)"
  policies="$(gcloud compute disks describe "$DATA_DISK" --project="$PROJECT" --zone="$ZONE" \
    --format='value(resourcePolicies)' 2>/dev/null || true)"
  if [[ "$policies" != *"/${SNAPSHOT_POLICY}"* ]]; then
    run gcloud compute disks add-resource-policies "$DATA_DISK" \
      --project="$PROJECT" --zone="$ZONE" --resource-policies="$SNAPSHOT_POLICY"
  fi
else
  # A disk created by this run is blank, so this run may format it.
  INIT_DATA_DISK=1
  run gcloud compute disks create "$DATA_DISK" \
    --project="$PROJECT" --zone="$ZONE" \
    --size="${DATA_DISK_GB}GB" --type=pd-standard \
    --resource-policies="$SNAPSHOT_POLICY" \
    --labels=role=ua-control-plane,task=2148
fi

# --- 3. firewall -------------------------------------------------------------
# The VM carries tag `tailscale`, which the existing rules already target:
# Tailscale UDP 41641 and SSH from the IAP range only. Nothing else is opened.
step "firewall (tag-scoped; no public app or MCP ports)"
ensure_rule() {
  local name="$1"; shift
  if exists gcloud compute firewall-rules describe "$name"; then
    echo "rule ${name} exists; keeping"
  else
    run gcloud compute firewall-rules create "$name" --project="$PROJECT" \
      --network="$NETWORK" --direction=INGRESS --priority=1000 --action=ALLOW \
      --target-tags=tailscale "$@"
  fi
}
ensure_rule "${UA_GCP_TS_RULE:-allow-tailscale-udp}" --rules=udp:41641 --source-ranges=0.0.0.0/0 \
  --description="Tailscale direct UDP for tailnet nodes"
ensure_rule "${UA_GCP_IAP_RULE:-allow-ssh-iap}" --rules=tcp:22 --source-ranges=35.235.240.0/20 \
  --description="SSH from Identity-Aware Proxy only"

# --- 4. VM -------------------------------------------------------------------
step "VM ${INSTANCE}"
if exists gcloud compute instances describe "$INSTANCE" --zone="$ZONE"; then
  echo "exists; keeping"
  gcloud compute instances describe "$INSTANCE" --project="$PROJECT" --zone="$ZONE" \
    --format='value(status,machineType.basename())'
else
  # Scopes: cloud-platform, bounded by the service account's IAM roles
  # (bucket-scoped objectCreator/objectViewer for the backup uploader).
  run gcloud compute instances create "$INSTANCE" \
    --project="$PROJECT" --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --image-family="$IMAGE_FAMILY" --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="${BOOT_DISK_GB}GB" --boot-disk-type=pd-balanced \
    --disk="name=${DATA_DISK},device-name=ua-data,mode=rw,boot=no,auto-delete=no" \
    --network-interface=network="${NETWORK}",no-address \
    --tags="$NETWORK_TAGS" \
    --service-account="$SERVICE_ACCOUNT" \
    --scopes=cloud-platform \
    --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --deletion-protection \
    --labels=role=ua-control-plane,task=2148
fi

if (( INFRA_ONLY == 1 )); then
  echo
  echo "infra-only: VM-side provisioning skipped."
  (( APPLY == 1 )) || echo "dry-run only; nothing was created"
  exit 0
fi

# --- 5. provision over IAP SSH ----------------------------------------------
SSH_FLAGS=(
  --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap --quiet
  --ssh-key-file="$SSH_KEY_FILE"
  --ssh-flag=-oIdentityAgent=none
  --ssh-flag=-oIdentitiesOnly=yes
  --ssh-flag=-oBatchMode=yes
  --ssh-flag=-oConnectTimeout=20
)
vm_ssh() { gcloud compute ssh "$INSTANCE" "${SSH_FLAGS[@]}" --command="$1"; }
STAGE=/root/ua-stage

# ship SRC DEST MODE: copy a local file to the VM over SSH stdin (no argv, no echo).
ship() {
  local src="$1" dest="$2" mode="$3"
  if (( APPLY == 1 )); then
    printf '+ ship %s -> %s:%s (mode %s)\n' "$(basename "$src")" "$INSTANCE" "$dest" "$mode"
    vm_ssh "sudo install -m ${mode} /dev/stdin ${dest}" <"$src"
  else
    printf '+ gcloud compute ssh %s --tunnel-through-iap --ssh-flag=-oIdentityAgent=none --command="sudo install -m %s /dev/stdin %s" < %s\n' \
      "$INSTANCE" "$mode" "$dest" "$src"
  fi
}

step "provision ${INSTANCE} over IAP SSH"
if (( APPLY == 1 )); then
  ok=0
  for _ in $(seq 1 30); do
    if vm_ssh true >/dev/null 2>&1; then ok=1; break; fi
    sleep 10
  done
  (( ok == 1 )) || die "no SSH path to ${INSTANCE} over IAP after 5 minutes"
fi
run_remote() {
  if (( APPLY == 1 )); then
    printf '+ [%s] %s\n' "$INSTANCE" "$1"
    vm_ssh "$1"
  else
    printf '+ gcloud compute ssh %s --tunnel-through-iap --ssh-flag=-oIdentityAgent=none --command="%s"\n' "$INSTANCE" "$1"
  fi
}
# The stage holds hub.env and the tailnet key. It is removed on every exit:
# by the remote trap around install.sh, and by this local trap if bootstrap
# stops before install.sh runs (a failed ship, a failed git archive).
cleanup_stage() {
  if (( APPLY == 1 )); then
    vm_ssh "sudo rm -rf -- ${STAGE}" >/dev/null 2>&1 \
      || echo "warning: could not remove ${STAGE} on ${INSTANCE}; remove it by hand" >&2
  fi
}
(( APPLY == 0 )) || trap cleanup_stage EXIT
run_remote "sudo install -d -m 0700 ${STAGE} ${STAGE}/control-plane"
for f in install.sh install-lib.sh verify.sh backup.sh ua-hub.service ua-hub-backup.service ua-hub-backup.timer; do
  ship "${SCRIPT_DIR}/${f}" "${STAGE}/control-plane/${f}" 0644
done
ship "${GCP_DIR}/sync-backup-to-gcs.sh" "${STAGE}/sync-backup-to-gcs.sh" 0755
ship "$ENV_FILE" "${STAGE}/hub.env" 0600
[[ -z "$TS_KEY_FILE" ]] || ship "$TS_KEY_FILE" "${STAGE}/tailscale-authkey" 0600

install_argv=(--apply "--stage=${STAGE}" "--ts-hostname=${INSTANCE}" "--gcp-project=${PROJECT}")
# Always explicit, so switching to local-only removes a stored destination.
install_argv+=("--backup-gcs-uri=${BACKUP_GCS_URI:-none}")
(( INIT_DATA_DISK == 0 )) || install_argv+=(--init-data-disk)
if [[ -n "$VCS_REF" ]]; then
  # The exact tree Railway builds from at this commit: git archive, no
  # working-tree changes, no credentials on the VM.
  if (( APPLY == 1 )); then
    printf '+ git archive %s | ship -> %s:%s/source.tar\n' "$VCS_REF" "$INSTANCE" "$STAGE"
    git -C "$REPO_ROOT" archive --format=tar "$VCS_REF" \
      | vm_ssh "sudo install -m 0600 /dev/stdin ${STAGE}/source.tar"
  else
    printf '+ git -C %s archive --format=tar %s | gcloud compute ssh %s ... "sudo install -m 0600 /dev/stdin %s/source.tar"\n' \
      "<repo>" "$VCS_REF" "$INSTANCE" "$STAGE"
  fi
  install_argv+=("--source-tar=${STAGE}/source.tar" "--vcs-ref=${VCS_REF}")
else
  install_argv+=("--image=${IMAGE}")
fi
# Build the remote command without letting any value reach shell evaluation:
# values were allowlisted above, and every word is quoted with printf %q.
# --cleanup-stage makes install.sh remove the stage (hub.env, the tailnet key,
# the source tarball) on exit, whether it succeeds or fails.
install_argv+=(--cleanup-stage)
run_remote "sudo bash $(printf '%q ' "${STAGE}/control-plane/install.sh" "${install_argv[@]}")"
trap - EXIT

# --- 6. verify -----------------------------------------------------------------
if (( SKIP_VERIFY == 0 )); then
  step "verify ${INSTANCE}"
  # First stand-up: the restart check is safe and proves state survives.
  run_remote "sudo /usr/local/lib/unified-agent/verify.sh --restart"
fi

echo
if (( APPLY == 1 )); then
  echo "control plane ${INSTANCE} is up. Record non-secret values in deploy/gcp/ops-state.md,"
  echo "then run the P2 device acceptance (#2112) against it."
else
  echo "dry-run only; nothing was created. Re-run with --apply to execute the plan above."
fi
