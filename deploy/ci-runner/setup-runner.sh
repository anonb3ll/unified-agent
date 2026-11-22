#!/usr/bin/env bash
# Self-hosted GitHub Actions runner for unified-agent CI on a GCP spot VM (task #1787).
#
#   setup-runner.sh gcloud-plan     Print the gcloud commands that create the VM
#                                   and copy this script to it. Runs no gcloud.
#   setup-runner.sh install         On the VM, as root: install the toolchain the
#                                   CI jobs expect, download and checksum the
#                                   runner, and start RUNNER_COUNT runner slots
#                                   as systemd services.
#   setup-runner.sh uninstall       On the VM, as root: stop and disable the slots.
#
# Trust model (read deploy/ci-runner/README.md before installing):
#   - Credentials stay root-only in /etc/ua-ci-runner (mode 700). Only a
#     root helper (systemd ExecStartPre=+) reads them, to register a slot or
#     mint a one-job JIT config. Job code never sees the registration token or
#     the PAT.
#   - Each slot runs as its own unprivileged user (ua-ci-<n>): no sudo, no
#     docker group, NoNewPrivileges. Slots cannot read each other's runner
#     credentials, temp files or caches.
#   - It is still one VM with a shared kernel, not a sandbox. Run only this
#     private repo's trusted code on it; never fork PRs.
#
# Configuration, all through the environment:
#   REPO            owner/name of the repository (required)
#   RUNNER_VERSION  actions/runner release, e.g. 2.329.0 (required for install)
#   RUNNER_SHA256   SHA-256 of actions-runner-linux-x64-$RUNNER_VERSION.tar.gz,
#                   from the release notes (required for install)
#   RUNNER_LABELS   extra labels (default ua-ci); match the UA_CI_RUNNER variable
#   RUNNER_COUNT    runner slots on this VM (default 4)
#   RUNNER_MODE     ephemeral (default; one job per registration, fresh slot
#                   directory, home and tmpfs each time) or persistent
#   TOKEN_FILE      registration token file (default /root/ua-runner-token)
#   PAT_FILE        optional fine-grained PAT file (this repo only,
#                   Administration: read and write). With it, ephemeral slots
#                   mint a JIT config per job and run indefinitely; without
#                   it they can register only while the registration token
#                   lives (one hour).
#   TMPFS_SIZE      per-slot pytest tmpfs (default 4g)
#   PROJECT ZONE NAME MACHINE_TYPE   gcloud-plan inputs
set -euo pipefail

ROOT_DIR=/opt/ua-ci-runner
SLOT_DIR=/var/lib/ua-ci-runner
STATE_DIR=/etc/ua-ci-runner

die() {
  echo "setup-runner: $*" >&2
  exit 1
}

usage() {
  sed -n '2,38p' "$0"
  exit "${1:-0}"
}

require_repo() {
  [ -n "${REPO:-}" ] || die "REPO (owner/name) is required"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "REPO must look like owner/name"
  [[ "${RUNNER_LABELS:-ua-ci}" =~ ^[A-Za-z0-9_.,-]+$ ]] || die "RUNNER_LABELS: letters, digits, . _ - and commas only"
}

gcloud_plan() {
  require_repo
  local project="${PROJECT:-<project>}" zone="${ZONE:-us-central1-a}"
  local name="${NAME:-ua-ci-runner-1}" machine="${MACHINE_TYPE:-e2-standard-8}"
  local labels="${RUNNER_LABELS:-ua-ci}"
  # Every value is rendered into the commands, so they can be pasted into a
  # fresh shell; nothing depends on variables set only for this script.
  cat <<EOF
# Review, then run these yourself. This script does not call gcloud.
# Spot VM, no service account and no API scopes: the runner only polls GitHub
# outbound, so it needs no Google credentials and no inbound firewall rule.
gcloud compute instances create ${name} \\
  --project=${project} --zone=${zone} \\
  --machine-type=${machine} \\
  --provisioning-model=SPOT --instance-termination-action=STOP \\
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \\
  --boot-disk-size=80GB --boot-disk-type=pd-balanced \\
  --no-service-account --no-scopes \\
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \\
  --metadata=enable-oslogin=TRUE \\
  --labels=role=ua-ci-runner

gcloud compute scp deploy/ci-runner/setup-runner.sh ${name}:setup-runner.sh \\
  --project=${project} --zone=${zone}

# Registration token: minted by you, piped straight into a root-only file.
# Nothing is printed. The token is valid for one hour.
gh api -X POST repos/${REPO}/actions/runners/registration-token --jq .token \\
  | gcloud compute ssh ${name} --project=${project} --zone=${zone} \\
      --command='sudo install -m 600 -o root -g root /dev/stdin /root/ua-runner-token'

# Optional, for ephemeral slots that outlive the hour: a fine-grained PAT for
# ${REPO} only (Administration: read and write), piped the same way.
#   <print-your-pat> | gcloud compute ssh ${name} --project=${project} --zone=${zone} \\
#       --command='sudo install -m 600 -o root -g root /dev/stdin /root/ua-runner-pat'

gcloud compute ssh ${name} --project=${project} --zone=${zone} --command=\\
  'sudo REPO=${REPO} RUNNER_LABELS=${labels} RUNNER_VERSION=<version> RUNNER_SHA256=<sha256> bash setup-runner.sh install'
# (add PAT_FILE=/root/ua-runner-pat to that command if you sent a PAT)

# Then point CI at it: gh variable set UA_CI_RUNNER --body ${labels} --repo ${REPO}
EOF
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # What the CI jobs assume a GitHub-hosted image provides, preinstalled
  # because the job users have no sudo: git, curl, jq, make and a compiler,
  # Python 3, bubblewrap for the sandboxed tests, rustup, and Node (only to
  # install Playwright's Chromium system libraries below). setup-uv and
  # setup-node fetch their own tool versions per job. No Docker: Docker access
  # is root on the host, and image jobs run on UA_CI_ISOLATED_RUNNER instead.
  apt-get install -y -qq \
    ca-certificates curl git jq make build-essential unzip zip \
    python3 python3-venv bubblewrap rustup nodejs npm
  # Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor,
  # which breaks bwrap; this VM runs only CI.
  printf 'kernel.apparmor_restrict_unprivileged_userns = 0\n' >/etc/sysctl.d/60-ua-ci-userns.conf
  sysctl -q --system || true
  # Chromium's shared libraries for the web accessibility gate.
  npx --yes playwright install-deps chromium >/dev/null
}

slot_user() {
  printf 'ua-ci-%s' "$1"
}

create_slot_user() {
  local user
  user="$(slot_user "$1")"
  if ! id "$user" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$user"
  fi
  # Deliberately no sudoers entry and no docker group.
  gpasswd -d "$user" docker >/dev/null 2>&1 || true
  rm -f "/etc/sudoers.d/90-$user"
  runuser -u "$user" -- rustup default stable >/dev/null 2>&1 || true
}

download_runner() {
  local tarball="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  local url="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tarball}"
  install -d -m 755 -o root -g root "$ROOT_DIR/dist"
  curl -fsSL "$url" -o "$ROOT_DIR/dist/$tarball"
  echo "${RUNNER_SHA256}  $ROOT_DIR/dist/$tarball" | sha256sum -c --quiet - \
    || die "runner tarball checksum mismatch; refusing to install"
  chmod 644 "$ROOT_DIR/dist/$tarball"
  ln -sf "$tarball" "$ROOT_DIR/dist/actions-runner.tar.gz"
}

write_state() {
  install -d -m 700 -o root -g root "$STATE_DIR"
  {
    printf 'REPO=%q\n' "$REPO"
    printf 'RUNNER_LABELS=%q\n' "${RUNNER_LABELS:-ua-ci}"
    printf 'RUNNER_MODE=%q\n' "${RUNNER_MODE:-ephemeral}"
    printf 'TMPFS_SIZE=%q\n' "${TMPFS_SIZE:-4g}"
  } >"$STATE_DIR/runner.env"
  chmod 600 "$STATE_DIR/runner.env"
  # Root-only copies; the slot users never get read access to either.
  install -m 600 -o root -g root "$TOKEN_FILE" "$STATE_DIR/registration-token"
  if [ -n "${PAT_FILE:-}" ]; then
    [ -s "$PAT_FILE" ] || die "PAT_FILE $PAT_FILE is empty or missing"
    install -m 600 -o root -g root "$PAT_FILE" "$STATE_DIR/pat"
  fi
}

write_helpers() {
  install -d -m 755 -o root -g root "$ROOT_DIR/bin"

  # Root helper (ExecStartPre=+): fresh slot directory, home and tmpfs, then
  # registration. Credentials are read here and never handed to the slot user.
  cat >"$ROOT_DIR/bin/prepare-slot" <<'PREP'
#!/usr/bin/env bash
set -euo pipefail
slot="$1"
. /etc/ua-ci-runner/runner.env
user="ua-ci-$slot"
dir="/var/lib/ua-ci-runner/slot-$slot"
tmp="/run/ua-ci/slot-$slot"
state=/etc/ua-ci-runner

install -d -m 711 -o root -g root /run/ua-ci /var/lib/ua-ci-runner
mountpoint -q "$tmp" && umount "$tmp"
install -d -m 700 -o "$user" -g "$user" "$tmp"
mount -t tmpfs -o "size=$TMPFS_SIZE,mode=0700,uid=$(id -u "$user"),gid=$(id -g "$user")" tmpfs "$tmp"

fresh=0
if [ "$RUNNER_MODE" = "ephemeral" ] || [ ! -f "$dir/.runner" ]; then
  fresh=1
  rm -rf "$dir"
  install -d -m 700 -o "$user" -g "$user" "$dir"
  runuser -u "$user" -- tar -xzf /opt/ua-ci-runner/dist/actions-runner.tar.gz -C "$dir"
  if [ "$RUNNER_MODE" = "ephemeral" ]; then
    # One job per slot lifetime: nothing a job leaves in this user's home
    # (caches included) reaches the next job.
    home="$(getent passwd "$user" | cut -d: -f6)"
    find "$home" -mindepth 1 -maxdepth 1 ! -name .rustup ! -name .cargo -exec rm -rf {} +
  fi
fi
# The runner exports .env to every job: the per-slot pytest tmpfs.
printf 'UA_CI_TMPFS=%s\n' "$tmp" >"$dir/.env"
chown "$user:$user" "$dir/.env"
rm -f "/run/ua-ci/slot-$slot.jit"
[ "$fresh" = 1 ] || exit 0

name="$(hostname)-$slot"
if [ "$RUNNER_MODE" = "ephemeral" ] && [ -s "$state/pat" ]; then
  # Mint a one-job JIT config. The PAT reaches curl in a root-only header file.
  work="$(mktemp -d /run/ua-ci/.mint-XXXXXX)"
  trap 'rm -rf "$work"' EXIT
  printf 'Authorization: Bearer %s\n' "$(cat "$state/pat")" >"$work/hdr"
  python3 - "$name" "$RUNNER_LABELS" >"$work/body" <<'PY'
import json, sys
labels = ["self-hosted", "linux", "x64", *[l for l in sys.argv[2].split(",") if l]]
print(json.dumps({"name": sys.argv[1], "runner_group_id": 1, "labels": labels, "work_folder": "_work"}))
PY
  curl -fsS -X POST -H @"$work/hdr" -H 'Accept: application/vnd.github+json' \
    --data @"$work/body" "https://api.github.com/repos/$REPO/actions/runners/generate-jitconfig" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["encoded_jit_config"])' >"$work/jit"
  # Per-slot file, atomically replaced: concurrent slots never share a path.
  chown "$user:$user" "$work/jit"
  chmod 400 "$work/jit"
  mv -f "$work/jit" "/run/ua-ci/slot-$slot.jit"
  exit 0
fi

# Registration-token path: config.sh runs as root (never as the slot user),
# with the token in its environment, which other users cannot read.
args=(--unattended --url "https://github.com/$REPO" --name "$name"
  --labels "$RUNNER_LABELS" --work _work --replace --disableupdate)
[ "$RUNNER_MODE" = "ephemeral" ] && args+=(--ephemeral)
(
  cd "$dir"
  RUNNER_ALLOW_RUNASROOT=1 ACTIONS_RUNNER_INPUT_TOKEN="$(cat "$state/registration-token")" \
    ./config.sh "${args[@]}" >/dev/null
)
chown -R "$user:$user" "$dir"
PREP

  # Runs as the slot user: start the runner on its JIT config or registration.
  cat >"$ROOT_DIR/bin/run-slot" <<'RUN'
#!/usr/bin/env bash
set -euo pipefail
slot="$1"
cd "/var/lib/ua-ci-runner/slot-$slot"
jit="/run/ua-ci/slot-$slot.jit"
if [ -s "$jit" ]; then
  cfg="$(cat "$jit")"
  rm -f "$jit"
  exec ./run.sh --jitconfig "$cfg"
fi
exec ./run.sh
RUN

  cat >"$ROOT_DIR/bin/cleanup-slot" <<'CLEAN'
#!/usr/bin/env bash
slot="$1"
umount "/run/ua-ci/slot-$slot" 2>/dev/null || true
rm -f "/run/ua-ci/slot-$slot.jit"
CLEAN
  chmod 755 "$ROOT_DIR/bin/prepare-slot" "$ROOT_DIR/bin/run-slot" "$ROOT_DIR/bin/cleanup-slot"

  cat >/etc/systemd/system/ua-ci-runner@.service <<UNIT
[Unit]
Description=unified-agent CI runner slot %i
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=ua-ci-%i
Group=ua-ci-%i
# "+" runs the helper as root: only it reads /etc/ua-ci-runner.
ExecStartPre=+$ROOT_DIR/bin/prepare-slot %i
ExecStart=$ROOT_DIR/bin/run-slot %i
ExecStopPost=+$ROOT_DIR/bin/cleanup-slot %i
# An ephemeral runner exits after its job; the restart prepares a fresh slot.
Restart=always
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=5min

[Install]
WantedBy=multi-user.target
UNIT
}

install_cmd() {
  [ "$(id -u)" -eq 0 ] || die "install must run as root"
  require_repo
  [ -n "${RUNNER_VERSION:-}" ] || die "RUNNER_VERSION is required"
  [ -n "${RUNNER_SHA256:-}" ] || die "RUNNER_SHA256 is required (copy it from the release notes)"
  TOKEN_FILE="${TOKEN_FILE:-/root/ua-runner-token}"
  [ -s "$TOKEN_FILE" ] || die "registration token file $TOKEN_FILE is empty or missing"
  case "${RUNNER_MODE:-ephemeral}" in ephemeral | persistent) ;; *) die "RUNNER_MODE must be ephemeral or persistent" ;; esac
  local count="${RUNNER_COUNT:-4}"
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || die "RUNNER_COUNT must be a positive integer"

  install_packages
  download_runner
  write_state
  write_helpers
  install -d -m 711 -o root -g root "$SLOT_DIR"
  # The install copies now live only in the root-only state directory.
  shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"
  if [ -n "${PAT_FILE:-}" ]; then
    shred -u "$PAT_FILE" 2>/dev/null || rm -f "$PAT_FILE"
  fi
  systemctl daemon-reload
  for slot in $(seq 1 "$count"); do
    create_slot_user "$slot"
    systemctl enable --now "ua-ci-runner@$slot.service"
  done
  echo "Started $count runner slot(s) with labels ${RUNNER_LABELS:-ua-ci} (${RUNNER_MODE:-ephemeral})."
  echo "Check: systemctl status 'ua-ci-runner@*'; then set the repo variable UA_CI_RUNNER."
}

uninstall_cmd() {
  [ "$(id -u)" -eq 0 ] || die "uninstall must run as root"
  systemctl disable --now 'ua-ci-runner@*.service' 2>/dev/null || true
  echo "Runner services stopped. Remove leftover runners in the repo settings"
  echo "(Settings > Actions > Runners) or with: gh api repos/<repo>/actions/runners"
}

case "${1:-}" in
  gcloud-plan) gcloud_plan ;;
  install) install_cmd ;;
  uninstall) uninstall_cmd ;;
  -h | --help | help) usage 0 ;;
  *) usage 1 ;;
esac
