#!/usr/bin/env bash
# VM-side install for the unified-agent cloud control plane (task #2148).
#
# Runs as root ON the control-plane VM. bootstrap.sh stages the inputs under
# --stage and calls this script; it can also be run by hand over SSH. It
# deploys the same artifact as Railway staging (the repo Dockerfile image, or
# a published image of it) with state on the data disk and the same env
# contract, and exposes the hub only through Tailscale Serve.
#
# Usage (as root on the VM):
#   install.sh [--apply] --stage=DIR (--source-tar=FILE --vcs-ref=SHA | --image=REF)
#              [--ts-hostname=NAME] [--backup-gcs-uri=gs://...|none] [--gcp-project=ID]
#              [--init-data-disk] [--cleanup-stage]
#
# --init-data-disk allows formatting the data disk, and only when every probe
# agrees it is blank (install-lib.sh). bootstrap.sh passes it on the run that
# creates the disk. --backup-gcs-uri=none removes a stored offsite destination.
# --cleanup-stage removes the stage directory (it holds hub.env and the tailnet
# key) when this script exits, on success or failure; bootstrap.sh passes it.
#
# Stage directory (written by bootstrap.sh, mode 0700):
#   hub.env            filled copy of hub.env.template (0600)       required
#   tailscale-authkey  one-time Tailscale auth key (0600)           first run only
#   source.tar         `git archive <sha>` of the repo              with --source-tar
#   control-plane/     this directory (install-lib.sh, units, backup.sh, verify.sh)
#   sync-backup-to-gcs.sh
#
# Without --apply nothing changes: every action is printed instead.
# Idempotent: an existing filesystem is never reformatted, an existing fstab
# entry is kept, a running tailnet login is kept, and re-running with the same
# image leaves the service running.
set -euo pipefail

APPLY=0
STAGE=""
SOURCE_TAR=""
VCS_REF=""
IMAGE=""
TS_HOSTNAME="ua-control-plane"
BACKUP_GCS_URI=""
GCP_PROJECT=""
INIT_DATA_DISK=0
CLEANUP_STAGE=0

DATA_DEVICE=/dev/disk/by-id/google-ua-data
DATA_MOUNT=/mnt/ua-data
FSTAB=/etc/fstab
ETC_DIR=/etc/unified-agent
LIB_DIR=/usr/local/lib/unified-agent
SRC_ROOT=/opt/unified-agent/src
HUB_UID=10001
HUB_PORT=8765

# Keys the unit pins or that would undo the hosted posture. An env file that
# sets any of them is refused rather than silently overridden.
FORBIDDEN_KEYS=(UA_HOSTED UA_EXECUTION_LOCALITY UA_STATE_MOUNT_PATH UA_HOME
  UA_ALLOW_EPHEMERAL_STATE UA_ALLOW_ROOT UA_DOCKER_ALLOW_INSECURE PORT
  UA_HTTP_HOST UA_HTTP_PORT UA_TRANSPORT UA_ENFORCE_AUTH)

usage() {
  sed -n '2,31p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --stage=*) STAGE="${1#*=}" ;;
    --source-tar=*) SOURCE_TAR="${1#*=}" ;;
    --vcs-ref=*) VCS_REF="${1#*=}" ;;
    --image=*) IMAGE="${1#*=}" ;;
    --ts-hostname=*) TS_HOSTNAME="${1#*=}" ;;
    --backup-gcs-uri=*) BACKUP_GCS_URI="${1#*=}" ;;
    --gcp-project=*) GCP_PROJECT="${1#*=}" ;;
    --init-data-disk) INIT_DATA_DISK=1 ;;
    --cleanup-stage) CLEANUP_STAGE=1 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
  shift
done

# shellcheck source=install-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install-lib.sh"

# --- inputs ------------------------------------------------------------------
[[ -n "$STAGE" && -d "$STAGE" ]] || die "--stage=DIR is required and must exist"
if (( CLEANUP_STAGE == 1 && APPLY == 0 )); then
  log "dry-run: would remove $STAGE on exit (--cleanup-stage); left in place"
elif (( CLEANUP_STAGE == 1 )); then
  # Only a dedicated staging directory may be deleted wholesale.
  [[ "$STAGE" == /* && "$(basename -- "$STAGE")" == ua-stage* ]] \
    || die "--cleanup-stage needs an absolute stage path named ua-stage*"
  trap 'rm -rf -- "$STAGE"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi
[[ -f "$STAGE/hub.env" ]] || die "missing $STAGE/hub.env (filled hub.env.template)"
if [[ -n "$IMAGE" && -n "$SOURCE_TAR" ]]; then
  die "pass --image or --source-tar, not both"
fi
if [[ -z "$IMAGE" && -z "$SOURCE_TAR" ]]; then
  die "pass --source-tar=FILE --vcs-ref=SHA (pinned checkout) or --image=REF"
fi
if [[ -n "$SOURCE_TAR" ]]; then
  [[ -f "$SOURCE_TAR" ]] || die "source tarball not found: $SOURCE_TAR"
  [[ "$VCS_REF" =~ ^[0-9a-f]{40}$ ]] || die "--vcs-ref must be a full 40-char commit SHA"
fi
if [[ -n "$BACKUP_GCS_URI" && "$BACKUP_GCS_URI" != gs://* && "$BACKUP_GCS_URI" != none ]]; then
  die "--backup-gcs-uri must be gs://... or none"
fi
CP_SRC="$STAGE/control-plane"
for f in ua-hub.service ua-hub-backup.service ua-hub-backup.timer backup.sh verify.sh; do
  [[ -f "$CP_SRC/$f" ]] || die "missing staged $CP_SRC/$f"
done
[[ -f "$STAGE/sync-backup-to-gcs.sh" ]] || die "missing staged sync-backup-to-gcs.sh"
if (( APPLY == 1 )) && [[ "$(id -u)" != "0" ]]; then
  die "run as root (sudo)"
fi
log "mode=$([[ $APPLY == 1 ]] && echo apply || echo dry-run) ts_hostname=$TS_HOSTNAME"

# --- 1. data disk ------------------------------------------------------------
prepare_data_disk

# --- 2. packages: docker + tailscale -----------------------------------------
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || die "expected Debian, found ${ID:-unknown}"
need_pkgs=()
command -v docker >/dev/null 2>&1 || need_pkgs+=(docker.io)
command -v curl >/dev/null 2>&1 || need_pkgs+=(curl)
if ((${#need_pkgs[@]})); then
  run apt-get update -q
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${need_pkgs[@]}"
fi
if ! command -v tailscale >/dev/null 2>&1; then
  codename="${VERSION_CODENAME:?no VERSION_CODENAME in /etc/os-release}"
  keyring=/usr/share/keyrings/tailscale-archive-keyring.gpg
  run curl -fsSL -o "$keyring" "https://pkgs.tailscale.com/stable/debian/${codename}.noarmor.gpg"
  run curl -fsSL -o /etc/apt/sources.list.d/tailscale.list \
    "https://pkgs.tailscale.com/stable/debian/${codename}.tailscale-keyring.list"
  run apt-get update -q
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q tailscale
fi
# The GCS client for the backup upload and Secret Manager references.
ensure_gcloud
run systemctl enable --now docker tailscaled

# --- 3. tailnet join (same pattern as ua-worker) ---------------------------
ts_state="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null \
  || true)"
if [[ "$ts_state" == "Running" ]]; then
  log "tailscale already up; keeping the existing login"
else
  [[ -f "$STAGE/tailscale-authkey" ]] || die "tailscale is not up and $STAGE/tailscale-authkey is missing"
  # file: keeps the key out of argv and out of this log.
  run tailscale up "--auth-key=file:$STAGE/tailscale-authkey" "--hostname=$TS_HOSTNAME"
fi
if [[ -f "$STAGE/tailscale-authkey" ]]; then
  run shred -u "$STAGE/tailscale-authkey"
fi

ts_dns=""
ts_ip=""
if (( APPLY == 1 )); then
  ts_dns="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  ts_ip="$(tailscale ip -4 | head -n 1)"
  [[ -n "$ts_dns" && -n "$ts_ip" ]] || die "could not read this node's MagicDNS name / tailnet IP"
  log "tailnet name: $ts_dns"
else
  ts_dns="<vm>.<tailnet>.ts.net"
  ts_ip="<tailnet-ip>"
fi

# --- 4. env files ------------------------------------------------------------
run install -d -m 0755 "$ETC_DIR"

# hub.env: rendered to a private temp file, moved into place only on success.
install_hub_env

# host.env: derived, non-secret defaults for the Tailscale Serve origin.
hub_has() { grep -qE "^$1=." "$STAGE/hub.env"; }
{
  echo "# Generated by install.sh (task #2148). Non-secret; hub.env overrides."
  hub_has UA_PUBLIC_URL || echo "UA_PUBLIC_URL=https://${ts_dns}"
  hub_has UA_PUBLIC_HOSTS || echo "UA_PUBLIC_HOSTS=${ts_dns},${ts_dns}:*,${ts_ip}:*"
} | write_file "$ETC_DIR/host.env" 0644

install_backup_env

# --- 5. image: the same artifact as Railway -----------------------------------
if [[ -n "$IMAGE" ]]; then
  image_ref="$IMAGE"
  run docker pull "$image_ref"
else
  image_ref="unified-agent:${VCS_REF}"
  if (( APPLY == 1 )) && docker image inspect "$image_ref" >/dev/null 2>&1; then
    log "image $image_ref already built"
  else
    src_dir="$SRC_ROOT/$VCS_REF"
    run install -d -m 0755 "$src_dir"
    run tar -x -f "$SOURCE_TAR" -C "$src_dir"
    run docker build --build-arg "VCS_REF=$VCS_REF" --build-arg "VERSION=${VCS_REF:0:12}" \
      -t "$image_ref" "$src_dir"
  fi
fi
previous_image=""
if [[ -f "$ETC_DIR/unit.env" ]]; then
  previous_image="$(sed -n 's/^UA_IMAGE=//p' "$ETC_DIR/unit.env")"
fi
printf 'UA_IMAGE=%s\n' "$image_ref" | write_file "$ETC_DIR/unit.env" 0644

# --- 6. systemd units + backup helpers ---------------------------------------
run install -d -m 0755 "$LIB_DIR"
run install -m 0755 "$CP_SRC/backup.sh" "$LIB_DIR/backup.sh"
run install -m 0755 "$CP_SRC/verify.sh" "$LIB_DIR/verify.sh"
run install -m 0755 "$STAGE/sync-backup-to-gcs.sh" "$LIB_DIR/sync-backup-to-gcs.sh"
for unit in ua-hub.service ua-hub-backup.service ua-hub-backup.timer; do
  run install -m 0644 "$CP_SRC/$unit" "/etc/systemd/system/$unit"
done
run systemctl daemon-reload
run systemctl enable ua-hub.service ua-hub-backup.timer
if systemctl is-active --quiet ua-hub.service; then
  # A re-run restarts so changed env files or a new image apply. State is on
  # the data disk, so a restart keeps it (verify.sh --restart proves that).
  log "restarting ua-hub (image ${previous_image:-none} -> $image_ref)"
  run systemctl restart ua-hub.service
else
  run systemctl start ua-hub.service
fi
run systemctl start ua-hub-backup.timer

# --- 7. Tailscale Serve: private ingress, exactly as the Mac hub --------------
# Raw TCP on :8765 (agents' MCP URL) and HTTPS :443 (Control Center, OAuth).
# Never Funnel: nothing here is reachable from the public internet.
run tailscale serve --bg "--tcp=$HUB_PORT" "tcp://127.0.0.1:$HUB_PORT"
run tailscale serve --bg --https=443 "http://127.0.0.1:$HUB_PORT"

# --- 8. wait for readiness ---------------------------------------------------
if (( APPLY == 1 )); then
  for _ in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HUB_PORT/readyz" || true)"
    if [[ "$code" == "200" ]]; then
      log "hub ready on 127.0.0.1:$HUB_PORT (image $image_ref)"
      log "tailnet: http://${ts_dns}:${HUB_PORT}/mcp  https://${ts_dns}/ui"
      exit 0
    fi
    sleep 2
  done
  journalctl -u ua-hub.service -n 40 --no-pager >&2 || true
  die "hub did not become ready within 180 s"
fi
log "dry-run complete; nothing was changed"
