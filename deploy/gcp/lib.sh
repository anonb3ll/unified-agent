#!/usr/bin/env bash
# Shared helpers for deploy/gcp/*.sh (sourced; not meant to be run directly).
# Naming defaults match deploy/gcp/CONTRACT.md.

# Prefer the desktop SDK install when present (interactive shells may omit it).
if [[ -d "$HOME/google-cloud-sdk/bin" ]]; then
  case ":${PATH}:" in
    *:"$HOME"/google-cloud-sdk/bin:*) ;;
    *) export PATH="$HOME/google-cloud-sdk/bin:${PATH}" ;;
  esac
fi

# Defaults from CONTRACT — ops replaces Live inventory; scripts accept overrides.
: "${UA_GCP_REGION:=us-central1}"
: "${UA_GCP_INSTANCE:=ua-worker}"
: "${UA_GCP_BACKUP_PREFIX:=memory-db}"

ua_gcp_require_gcloud() {
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: gcloud not on PATH. Add $HOME/google-cloud-sdk/bin or install the SDK." >&2
    exit 127
  fi
}

ua_gcp_project() {
  # Prefer explicit env; else gcloud config; never invent a live project id.
  if [[ -n "${UA_GCP_PROJECT:-}" ]]; then
    printf '%s\n' "$UA_GCP_PROJECT"
    return 0
  fi
  local cfg
  cfg="$(gcloud config get-value project 2>/dev/null || true)"
  if [[ -n "$cfg" && "$cfg" != "(unset)" ]]; then
    printf '%s\n' "$cfg"
    return 0
  fi
  echo "error: set UA_GCP_PROJECT (e.g. ua-worker-<short>) or gcloud config set project" >&2
  echo "       Live inventory in CONTRACT.md / ops-state.md is filled by Stream A." >&2
  exit 1
}

ua_gcp_require_project() {
  local project="$1"
  if [[ "$project" == *"<"* || "$project" == *TBD* ]]; then
    echo "error: project_id still a placeholder (${project}); fill Live inventory first" >&2
    exit 1
  fi
}

ua_gcp_bucket_name() {
  # Prefer explicit bucket; else live task #522 default; else derive from suffix.
  if [[ -n "${UA_GCP_BACKUP_BUCKET:-}" ]]; then
    printf '%s\n' "$UA_GCP_BACKUP_BUCKET"
    return 0
  fi
  local project short
  project="$(ua_gcp_project)"
  # Live inventory (2026-08-21): project ua-worker → this bucket.
  if [[ "$project" == "ua-worker" ]]; then
    printf 'ua-worker-backups-000000000000\n'
    return 0
  fi
  short="${project#ua-worker-}"
  if [[ "$short" == "$project" || -z "$short" ]]; then
    echo "error: set UA_GCP_BACKUP_BUCKET" >&2
    echo "       or use project ua-worker / ua-worker-<short>" >&2
    exit 1
  fi
  printf 'ua-worker-backups-%s\n' "$short"
}

ua_gcp_dry_run() {
  # Print the would-be command; caller decides whether to execute.
  printf '+ %s\n' "$*"
}
