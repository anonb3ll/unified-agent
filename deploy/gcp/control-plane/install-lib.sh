# shellcheck shell=bash
# Functions for install.sh (task #2148). Sourced, not run. Kept separate so the
# steps that can destroy or leak state (formatting the data disk, writing the
# secret env file, switching off offsite backups) are tested on their own with
# stub commands on PATH; see tests/test_gcp_control_plane.py.
#
# Callers set: APPLY (0|1), DATA_DEVICE, DATA_MOUNT, FSTAB, HUB_UID,
# INIT_DATA_DISK (0|1), ETC_DIR, STAGE, GCP_PROJECT, BACKUP_GCS_URI,
# FORBIDDEN_KEYS (array).

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] error: %s\n' "$*" >&2; exit 1; }

# run CMD...: execute under --apply, print otherwise. Never pass secrets as
# arguments: they would show in the dry-run output and in /proc.
run() {
  if (( APPLY == 1 )); then
    printf '+ %s\n' "$*"
    "$@"
  else
    printf 'would: %s\n' "$*"
  fi
}

# write_file PATH MODE: stdin -> PATH atomically with MODE. For non-secret
# content only; secret files go through install_hub_env.
write_file() {
  local path="$1" mode="$2" tmp
  if (( APPLY == 0 )); then
    printf 'would: write %s (mode %s)\n' "$path" "$mode"
    cat >/dev/null
    return 0
  fi
  tmp="$(mktemp "${path}.XXXXXX")"
  chmod "$mode" "$tmp"
  if ! cat >"$tmp"; then
    rm -f "$tmp"
    die "could not write $path"
  fi
  mv -f "$tmp" "$path"
  printf '+ wrote %s (mode %s)\n' "$path" "$mode"
}

# --- data disk -----------------------------------------------------------------

# data_disk_state DEVICE -> prints "ext4", "blank", or "refuse:<reason>".
# "blank" needs three independent signs: blkid finds no signature (exit 2,
# and only exit 2), wipefs lists none, and lsblk shows the bare disk with no
# filesystem and no partitions. Anything else, including any probe error, is
# a refusal: an unreadable disk is not an empty one.
data_disk_state() {
  local dev="$1" rc fstype wipe lsb
  rc=0
  fstype="$(blkid -p -s TYPE -o value "$dev" 2>/dev/null)" || rc=$?
  case "$rc" in
    0)
      if [[ "$fstype" == ext4 ]]; then
        echo ext4
      else
        echo "refuse:the disk holds a '${fstype:-unknown}' signature, not ext4"
      fi
      return 0
      ;;
    2) ;;
    *)
      echo "refuse:blkid probe failed (exit $rc); the disk may be unreadable or ambiguous"
      return 0
      ;;
  esac
  if ! wipe="$(wipefs --no-act "$dev" 2>/dev/null)"; then
    echo "refuse:wipefs probe failed"
    return 0
  fi
  if [[ -n "${wipe//[[:space:]]/}" ]]; then
    echo "refuse:wipefs reports signatures blkid did not"
    return 0
  fi
  if ! lsb="$(lsblk -n -r -o TYPE,FSTYPE "$dev" 2>/dev/null)"; then
    echo "refuse:lsblk probe failed"
    return 0
  fi
  # Exactly one row: the disk itself, no filesystem type, no partitions.
  if [[ "$(printf '%s\n' "$lsb" | grep -c .)" != 1 || ! "$lsb" =~ ^disk[[:space:]]*$ ]]; then
    echo "refuse:lsblk shows partitions or a filesystem"
    return 0
  fi
  echo blank
}

# prepare_data_disk: format only a provably blank disk, and only with
# --init-data-disk (bootstrap.sh passes it on the run that creates the disk).
# Then fstab, mount, ownership. Never reformats.
prepare_data_disk() {
  local state uuid
  [[ -e "$DATA_DEVICE" ]] || die "data disk $DATA_DEVICE not attached (device-name must be ua-data)"
  state="$(data_disk_state "$DATA_DEVICE")"
  case "$state" in
    ext4)
      log "data disk already has an ext4 filesystem; not formatting"
      ;;
    blank)
      if (( INIT_DATA_DISK != 1 )); then
        die "data disk $DATA_DEVICE is blank. Pass --init-data-disk to format it (first stand-up only)"
      fi
      run mkfs.ext4 -q -m 0 -L ua-data -E lazy_itable_init=0,lazy_journal_init=0,discard "$DATA_DEVICE"
      ;;
    refuse:*)
      die "refusing to touch data disk $DATA_DEVICE: ${state#refuse:}. Inspect it by hand; nothing was formatted"
      ;;
    *)
      die "unexpected data disk state '$state'; nothing was formatted"
      ;;
  esac
  run mkdir -p "$DATA_MOUNT"
  if grep -qsE "^[^#]*[[:space:]]${DATA_MOUNT}[[:space:]]" "$FSTAB"; then
    log "fstab already mounts $DATA_MOUNT"
  elif (( APPLY == 1 )); then
    uuid="$(blkid -s UUID -o value "$DATA_DEVICE")" || die "could not read the data disk UUID"
    [[ -n "$uuid" ]] || die "could not read the data disk UUID"
    printf 'UUID=%s %s ext4 defaults,discard,nofail 0 2\n' "$uuid" "$DATA_MOUNT" >>"$FSTAB"
    log "added fstab entry for $DATA_MOUNT"
  else
    printf 'would: append UUID=<data disk> %s ext4 defaults,discard,nofail 0 2 to %s\n' "$DATA_MOUNT" "$FSTAB"
  fi
  if mountpoint -q "$DATA_MOUNT"; then
    log "$DATA_MOUNT already mounted"
  else
    run mount "$DATA_MOUNT"
  fi
  # The image runs as uid 10001; the disk root is its /data.
  run chown "$HUB_UID:$HUB_UID" "$DATA_MOUNT"
  run chmod 0750 "$DATA_MOUNT"
}

# --- GCS client -----------------------------------------------------------------

# ensure_gcloud: backup.sh (through sync-backup-to-gcs.sh) and secretmanager:
# references need gcloud. GCE Debian images ship it; keep that. Otherwise
# install google-cloud-cli from Google's apt repository, pinned to Google's
# key in its own keyring (signed-by), never a global trusted key.
ensure_gcloud() {
  local keyring="${KEYRING_DIR:-/usr/share/keyrings}/cloud.google.gpg"
  local list="${APT_SOURCES_DIR:-/etc/apt/sources.list.d}/google-cloud-sdk.list"
  if command -v gcloud >/dev/null 2>&1; then
    log "gcloud present; keeping it"
    return 0
  fi
  if ! command -v gpg >/dev/null 2>&1; then
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q gnupg
  fi
  if (( APPLY == 1 )); then
    printf '+ fetch Google Cloud apt key -> %s\n' "$keyring"
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --batch --yes --dearmor -o "$keyring" \
      || die "could not fetch the Google Cloud apt signing key"
  else
    printf 'would: fetch https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o %s\n' "$keyring"
  fi
  printf 'deb [signed-by=%s] https://packages.cloud.google.com/apt cloud-sdk main\n' "$keyring" \
    | write_file "$list" 0644
  run apt-get update -q
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q google-cloud-cli
  if (( APPLY == 1 )) && ! command -v gcloud >/dev/null 2>&1; then
    die "google-cloud-cli installed but gcloud is not on PATH"
  fi
}

# --- env files -----------------------------------------------------------------

# render_hub_env: $STAGE/hub.env -> stdout. Drops comments and empty values
# (an empty docker env value would blank an image default), refuses pinned
# keys, resolves secretmanager: references. Values are never printed to the
# log; only key names go to stderr. Every failure returns non-zero explicitly:
# callers run it under `if !`, where errexit is off, so an unchecked write
# error (a full disk) would otherwise still report success.
render_hub_env() {
  local line key value secret version f keys=()
  [[ -r "$STAGE/hub.env" ]] || die "hub.env: $STAGE/hub.env is missing or unreadable"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || die "hub.env: malformed line (expected KEY=VALUE)"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ -z "$value" ]] && continue
    for f in "${FORBIDDEN_KEYS[@]}"; do
      [[ "$key" == "$f" ]] && die "hub.env sets $key; the unit pins it, remove the line"
    done
    [[ "$key" == RAILWAY_* ]] && die "hub.env sets $key; Railway variables do not belong on this host"
    if [[ "$value" == secretmanager:* ]]; then
      secret="${value#secretmanager:}"
      version="latest"
      if [[ "$secret" == *:* ]]; then
        version="${secret##*:}"
        secret="${secret%%:*}"
      fi
      [[ "$secret" =~ ^[A-Za-z0-9_-]+$ ]] || die "hub.env: $key has a malformed secretmanager reference"
      if (( APPLY == 1 )); then
        value="$(gcloud secrets versions access "$version" --secret="$secret" \
          ${GCP_PROJECT:+--project="$GCP_PROJECT"} 2>/dev/null)" \
          || die "hub.env: could not read Secret Manager secret for $key (service-account access?)"
        [[ -n "$value" ]] || die "hub.env: Secret Manager secret for $key is empty"
        [[ "$value" != *$'\n'* ]] || die "hub.env: secret for $key spans lines; env files hold one line"
      fi
    fi
    keys+=("$key")
    printf '%s=%s\n' "$key" "$value" || return 1
  done <"$STAGE/hub.env" || return 1
  printf '[install] hub.env keys: %s\n' "${keys[*]:-(none)}" >&2
  return 0
}

# install_hub_env: render into a private temp file beside the target and move
# it into place only if the whole render succeeded. On any failure the old
# $ETC_DIR/hub.env is left untouched and the install stops.
install_hub_env() {
  local target="$ETC_DIR/hub.env" tmp
  if (( APPLY == 0 )); then
    # Still validates the file (pinned keys, malformed lines) in a dry run.
    ( render_hub_env ) >/dev/null || die "hub.env did not render; nothing written"
    printf 'would: write %s (mode 0600)\n' "$target"
    return 0
  fi
  tmp="$(umask 077 && mktemp "${target}.XXXXXX")" || die "could not create a temp file in $ETC_DIR"
  chmod 0600 "$tmp"
  if ! ( render_hub_env ) >"$tmp"; then
    rm -f "$tmp"
    die "hub.env did not render; kept the existing $target"
  fi
  if ! mv -f "$tmp" "$target"; then
    rm -f "$tmp"
    die "could not move the rendered hub.env into place; kept the existing $target"
  fi
  printf '+ wrote %s (mode 0600)\n' "$target"
}

# install_backup_env: the offsite destination for backup.sh.
#   gs://...  write it;  "none"  remove it (local-only);  ""  leave it as is.
install_backup_env() {
  local target="$ETC_DIR/backup.env"
  case "$BACKUP_GCS_URI" in
    "")
      log "backup destination unchanged (pass --backup-gcs-uri=gs://... or none to set it)"
      ;;
    none)
      if [[ -e "$target" ]]; then
        run rm -f "$target"
      fi
      log "backups are local-only; no offsite copy"
      ;;
    gs://*)
      printf 'UA_CP_BACKUP_GCS_URI=%s\n' "$BACKUP_GCS_URI" | write_file "$target" 0644
      ;;
    *)
      die "--backup-gcs-uri must be gs://... or none"
      ;;
  esac
}
