#!/usr/bin/env bash
# Steward daily security audit (deterministic — no LLM involved).
#
# Runs `unified-agent-audit surfaces --fail-on high --file-tasks` (A–F grade,
# allowlisted roots, existing audit engine). Appends a one-line result to
# ~/.unified_agent/audit.log, files [audit-triage:] tasks, and on failure
# writes a findings report into the vault inbox. Exits 1 when the high gate
# trips so the timer is visibly red.
#
# Idempotent: if the combined failing-root report is byte-identical to the
# last report recorded today, the inbox write and iMessage alert are skipped
# (a SKIP-DUPLICATE line is still logged) so repeat invocations on an
# unchanged system — the scheduled run plus any manual re-runs for testing —
# don't keep appending duplicate content to the same day's inbox note
# (task #119).
#
# Install:  crontab -e  →  0 9 * * * $HOME/dev/unified_agent/deploy/mac-hub/run-steward-audit.sh
set -u

run_kind=manual
case $# in
  0) ;;
  1)
    if [[ "$1" == "--scheduled" ]]; then
      run_kind=scheduled
    else
      echo "usage: $0 [--scheduled]" >&2
      exit 2
    fi
    ;;
  *)
    echo "usage: $0 [--scheduled]" >&2
    exit 2
    ;;
esac

AUDIT_BIN="${UA_CODE_ROOT:-$HOME/dev/unified_agent}/.venv/bin/unified-agent-audit"
LOG="$HOME/.unified_agent/audit.log"
INBOX="$HOME/vaults/main/00 - Inbox"
STATE_DIR="$HOME/.unified_agent"
LAST_DATE_FILE="$STATE_DIR/steward-audit-last-report.date"
LAST_HASH_FILE="$STATE_DIR/steward-audit-last-report.sha256"
LAST_ALERT_DATE_FILE="$STATE_DIR/steward-audit-last-alert.date"
LAST_ALERT_HASH_FILE="$STATE_DIR/steward-audit-last-alert.sha256"

# Retain at most 4096 bytes in memory. Keep draining after the prefix so a
# noisy notifier finishes normally instead of receiving SIGPIPE. No raw
# stderr file is created and the shell never captures the discarded suffix.
ua_steward_audit_bounded_error() {
  LC_ALL=C head -c 4096
  cat >/dev/null
}

# Only fixed categories leave this process; never persist notifier stderr,
# recipient handles, provider credentials or message content (task #1891).
ua_steward_audit_alert_error_kind() {
  # GNU timeout reports 124 even when it emits no error text (notify.sh).
  if [[ "${2:-1}" == 124 ]]; then
    printf '%s' timeout
    return
  fi
  # Recognize only a diagnostic at the start of the first stderr line.
  # An echoed body or later line must not turn a phrase into an error type.
  local error="${1%%$'\n'*}"
  error="${error#"${error%%[![:space:]]*}"}"
  case "$error" in
    "send: failed — "*) error="${error#send: failed — }" ;;
    "send: "*) error="${error#send: }" ;;
    "notify.sh: "*) error="${error#notify.sh: }" ;;
  esac
  case "$error" in
    RateLimitError|RateLimitError:*|RateLimitError" "*) printf '%s' rate_limited ;;
    "send timeout"*|"Error: send timeout"*|TimeoutError|TimeoutError:*|TimeoutError" "*) printf '%s' timeout ;;
    "missing SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET"*|"no NOTIFY_TARGET or BRIDGE_ALLOWED_SENDERS"*|"bun not found"*) printf '%s' configuration_error ;;
    AuthenticationError|AuthenticationError:*|AuthenticationError" "*|UnauthorizedError|UnauthorizedError:*|UnauthorizedError" "*) printf '%s' authentication_error ;;
    ConnectionError|ConnectionError:*|ConnectionError" "*|ECONNREFUSED*|ENOTFOUND*|"Error: connect ECONNREFUSED"*) printf '%s' transport_error ;;
    *) printf '%s' sender_failed ;;
  esac
}

# AgentShield-lite: one read-only surfaces scan (A–F grade, high-severity
# gate, [audit-triage:] task filing). Roots come from UA_AUDIT_ROOTS when set,
# else the built-in surface list (dev/agent, unified_agent, host agent dirs).
# Extra --allowlist entries keep the previous hardcoded set in scope when the
# env var is unset; when UA_AUDIT_ROOTS is set it is replace-not-append and
# these flags are ignored unless they sit under that allowlist.

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
source "$(dirname "${BASH_SOURCE[0]}")/steward-audit-status.sh"
if [[ "$run_kind" == scheduled && -z "${UA_STEWARD_AUDIT_RUN_ID:-}" ]]; then
    # A CLI flag alone does not prove this process came from the timer wrapper.
    run_kind=manual
fi

now=$(date -u +%FT%TZ)
today=$(date -u +%F)
report=""

# Let system_doctor tie the latest surfaces result to this invocation. A
# startup failure after this marker must not reuse an older FAIL result.
echo "$now START steward-audit kind=$run_kind" >>"$LOG"
[ -x "$AUDIT_BIN" ] || { echo "steward-audit-cron: $AUDIT_BIN not executable" >&2; exit 2; }

out=$("$AUDIT_BIN" surfaces --fail-on high --file-tasks 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
    echo "$now PASS surfaces" >>"$LOG"
    [[ "$run_kind" != scheduled ]] || ua_steward_audit_status_finish "$STATE_DIR" surfaces_pass 0
else
    echo "$now FAIL surfaces rc=$rc" >>"$LOG"
    report+=$'\n## surfaces\n\n```\n'"$out"$'\n```\n'
fi

if command -v sha256sum >/dev/null 2>&1; then
    _sha256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
    _sha256() { shasum -a 256 "$@"; }
else
    # openssl without -r prints "SHA2-256(stdin)= <digest>"; -r is coreutils form.
    _sha256() { openssl dgst -sha256 -r "$@" | awk '{print $1}'; }
fi

[ -n "$report" ] || exit 0

report_hash=$(printf '%s' "$report" | _sha256 | awk '{print $1}')
last_date=""
last_hash=""
[ -f "$LAST_DATE_FILE" ] && last_date=$(cat "$LAST_DATE_FILE")
[ -f "$LAST_HASH_FILE" ] && last_hash=$(cat "$LAST_HASH_FILE")
delivery_failed=0

if [ "$today" != "$last_date" ] || [ "$report_hash" != "$last_hash" ]; then
  if [ -d "$INBOX" ]; then
    note="$INBOX/Security Audit $today.md"
    if [ ! -f "$note" ]; then
        printf -- '---\ndate: %s\nsource: steward-audit-cron\ntags: [security, steward]\n---\n\n# Security audit findings — %s\n' \
            "$today" "$today" >"$note" || delivery_failed=1
    fi
    if [ "$delivery_failed" -eq 0 ]; then
      printf -- '\n*Run at %s (high-severity gate).*\n%s' "$now" "$report" >>"$note" || delivery_failed=1
    fi
  else
    echo "$now ERROR audit findings inbox missing" >>"$LOG"
    delivery_failed=1
  fi
  if [ "$delivery_failed" -eq 0 ]; then
    printf '%s' "$today" >"$LAST_DATE_FILE" || delivery_failed=1
    printf '%s' "$report_hash" >"$LAST_HASH_FILE" || delivery_failed=1
  fi
else
  echo "$now SKIP-DUPLICATE (report unchanged since last run today)" >>"$LOG"
fi

# Alert delivery has its own marker: a failed notification can retry without
# appending the same report to the inbox again.
last_alert_date=""
last_alert_hash=""
[ -f "$LAST_ALERT_DATE_FILE" ] && last_alert_date=$(cat "$LAST_ALERT_DATE_FILE")
[ -f "$LAST_ALERT_HASH_FILE" ] && last_alert_hash=$(cat "$LAST_ALERT_HASH_FILE")
if [ "$today" != "$last_alert_date" ] || [ "$report_hash" != "$last_alert_hash" ]; then
  alert_failed=0
  NOTIFY_SH="$HOME/dev/agent/projects/imessage-bridge/notify.sh"
  if [ -x "$NOTIFY_SH" ]; then
    failed_roots=$(grep "$now FAIL" "$LOG" 2>/dev/null | awk '{print $3}')
    alert_started_at=$(date -u +%FT%TZ)
    if alert_error=$(
      {
        printf '⚠️ Security Audit Failed!\n\nFailed roots:\n%s\n\nCheck audit.log and the Obsidian inbox if available.' "$failed_roots" | "$NOTIFY_SH" 2>&1 >/dev/null
        exit "${PIPESTATUS[1]}"
      } | ua_steward_audit_bounded_error
      # The collector's status must not replace the actual notifier status.
      exit "${PIPESTATUS[0]}"
    ); then
      :
    else
      alert_exit=$?
      alert_finished_at=$(date -u +%FT%TZ)
      # Inspect only a bounded prefix in memory and emit an allowlisted label.
      # A raw-error file or regex redaction could retain an unknown secret shape.
      alert_error_kind=$(ua_steward_audit_alert_error_kind "$alert_error" "$alert_exit")
      alert_run_id=manual
      if [[ "$run_kind" == scheduled && "${UA_STEWARD_AUDIT_RUN_ID:-}" =~ ^steward-audit-run\.[A-Za-z0-9]{6}$ ]]; then
        alert_run_id=$UA_STEWARD_AUDIT_RUN_ID
      fi
      echo "$alert_finished_at ERROR audit findings alert failed run_id=$alert_run_id notify_exit=$alert_exit error_kind=$alert_error_kind started_at=$alert_started_at finished_at=$alert_finished_at" >>"$LOG"
      alert_failed=1
      delivery_failed=1
    fi
    unset alert_error
  fi
  if [ "$alert_failed" -eq 0 ]; then
    # Each successful channel keeps its marker even if the other channel fails.
    printf '%s' "$today" >"$LAST_ALERT_DATE_FILE" || delivery_failed=1
    printf '%s' "$report_hash" >"$LAST_ALERT_HASH_FILE" || delivery_failed=1
  fi
fi
if [ "$delivery_failed" -ne 0 ]; then
  [[ "$run_kind" != scheduled ]] || ua_steward_audit_status_finish "$STATE_DIR" delivery_fail 2 "$rc"
  exit 2
fi
[[ "$run_kind" != scheduled ]] || ua_steward_audit_status_finish "$STATE_DIR" surfaces_fail "$rc"
exit "$rc"
