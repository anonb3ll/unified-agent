#!/usr/bin/env bash
# Bounded, run-correlated status for the scheduled steward audit.

ua_steward_audit_status_begin() {
  local state_dir="$1" status_file="$1/steward-audit-status"
  local lock_dir="$1/steward-audit-status.lock" run_dir run_id pointer_tmp attempt
  local lock_acquired=0
  mkdir -p "$state_dir"

  # Serialize only marker publication, never the audit itself. A crashed
  # publisher can leave this short-lived lock behind; bound waiting and fail
  # closed by leaving the previous evidence unusable to this invocation.
  for attempt in {1..100}; do
    if mkdir "$lock_dir" 2>/dev/null; then
      lock_acquired=1
      break
    fi
    sleep 0.01
  done
  if [[ "$lock_acquired" != 1 ]]; then
    return 1
  fi

  # Invalidate previous evidence while owning the publication lock. If
  # creating the per-run directory fails, doctor remains fail-closed.
  pointer_tmp="$status_file.tmp.$$"
  printf 'kind=scheduled\nrun_id=unavailable%s\n' "$$" >"$pointer_tmp" || {
    rmdir "$lock_dir"
    return 1
  }
  mv "$pointer_tmp" "$status_file" || {
    rm -f "$pointer_tmp"
    rmdir "$lock_dir"
    return 1
  }

  # Keep only the run records present at the beginning of this publication.
  # A later start replaces the pointer, so older completions cannot become
  # current again even if they finish last.
  run_dir=$(mktemp -d "$state_dir/steward-audit-run.XXXXXX") || {
    rmdir "$lock_dir"
    return 1
  }
  run_id=${run_dir##*/}
  printf 'kind=scheduled\nrun_id=%s\nstate=started\n' "$run_id" >"$run_dir/status.tmp" || {
    rmdir "$lock_dir"
    return 1
  }
  mv "$run_dir/status.tmp" "$run_dir/status" || {
    rmdir "$lock_dir"
    return 1
  }
  pointer_tmp="$status_file.tmp.$$"
  printf 'kind=scheduled\nrun_id=%s\n' "$run_id" >"$pointer_tmp" || {
    rmdir "$lock_dir"
    return 1
  }
  mv "$pointer_tmp" "$status_file" || {
    rm -f "$pointer_tmp"
    rmdir "$lock_dir"
    return 1
  }
  for attempt in "$state_dir"/steward-audit-run.*; do
    [[ "$attempt" == "$run_dir" || ! -d "$attempt" ]] || rm -rf "$attempt"
  done
  rmdir "$lock_dir"
  UA_STEWARD_AUDIT_RUN_ID="$run_id"
  export UA_STEWARD_AUDIT_RUN_ID
}

ua_steward_audit_status_finish() {
  # Optional 4th argument: the audit binary's own exit code, recorded as
  # audit_exit_code so a delivery failure cannot hide a failed scan.
  local state_dir="$1" state="$2" exit_code="$3" audit_exit_code="${4:-}"
  local status_file="$state_dir/steward-audit-status"
  local run_id="${UA_STEWARD_AUDIT_RUN_ID:-}" record="$state_dir/${UA_STEWARD_AUDIT_RUN_ID:-}/status"
  [[ "$run_id" == steward-audit-run.* && -f "$record" ]] || return 0
  grep -Fqx "run_id=$run_id" "$status_file" || return 0
  {
    printf 'kind=scheduled\nrun_id=%s\nstate=%s\nexit_code=%s\n' \
      "$run_id" "$state" "$exit_code"
    [[ -z "$audit_exit_code" ]] || printf 'audit_exit_code=%s\n' "$audit_exit_code"
  } >"$record.tmp.$$"
  mv "$record.tmp.$$" "$record"
}
