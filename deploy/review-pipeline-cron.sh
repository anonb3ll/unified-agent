#!/usr/bin/env bash
# Automated review pipeline driver (deterministic — no LLM involved).
#
# Runs `unified-agent-review scan --json` against the shared workspace, appends
# a one-line result to ~/.unified_agent/review.log, and on any red-tier or
# failed verdict writes the full findings into the vault inbox so they surface
# for Operator even if the CLI's own iMessage push failed. The CLI (not this
# script) sends the per-task iMessage notification — see docs/automation.md.
#
# Install:  crontab -e  →  */15 * * * * $HOME/dev/unified_agent/deploy/review-pipeline-cron.sh
# Prefer the systemd timer on hub hosts: deploy/systemd/ua-review-pipeline.timer
set -u

# Cron/systemd user services do not inherit the interactive shell PATH.
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:${PATH:-/usr/bin:/bin:/usr/local/bin}"

REVIEW_BIN="${UA_CODE_ROOT:-$HOME/dev/unified_agent}/.venv/bin/unified-agent-review"
UA_HOME_DIR="${UA_HOME:-$HOME/.unified_agent}"
LOG="$UA_HOME_DIR/review.log"
INBOX="$HOME/vaults/main/00 - Inbox"

mkdir -p "$(dirname "$LOG")"

# A retired desktop has no local task store and performs no scan. Check this
# before requiring the review venv or acquiring a lock.
if [ -f "$UA_HOME_DIR/NO_LOCAL_MEMORY_DB" ]; then
    now=$(date -u +%FT%TZ)
    echo "$now OK skipped — local task store retired (NO_LOCAL_MEMORY_DB)" >>"$LOG"
    exit 0
fi

[ -x "$REVIEW_BIN" ] || { echo "review-pipeline-cron: $REVIEW_BIN not executable" >&2; exit 2; }

# Single-instance lock (task #1254 Devin review, PR #691): raising the pytest
# gate's ceiling from a hardcoded 600s to a configurable default of 1800s
# (UA_REVIEW_PYTEST_TIMEOUT_S) means a genuinely slow/stuck run can now
# outlast this wrapper's own */15-minute cadence (crontab, ua-review-pipeline
# .timer, and the Mac LaunchAgent's StartInterval=900 all funnel into this
# one script — see deploy/mac-hub/run-review-pipeline.sh). Without a
# lock, an overlapping invocation reads the same pre-timeout note count as
# the run still in flight, so reviewer.py's per-sha retry cap
# (UA_REVIEW_TIMEOUT_MAX_RETRIES) is bypassed: N processes race, each thinks
# it holds the Nth-or-earlier attempt, each can send its own "first timeout"
# notification, and multiple pytest runs pile up on the same 4-core host.
# Python's fcntl.flock works on both macOS and Linux. The lock belongs to
# this shell's open file descriptor and the kernel releases it on exit, even
# after a crash. No age-based reclaim can steal a live, slow scan's lock.
#
# SCOPE NOTE: this lock covers only invocations funneling through this
# wrapper. A manual `unified-agent-review task <id>` retry or a direct
# `scan` run concurrently with a cron scan bypasses it and can double-run
# gates / double-notify on the same pre-run timeout count.
REVIEW_PYTHON="${REVIEW_BIN%/*}/python"
if [ ! -x "$REVIEW_PYTHON" ]; then
    REVIEW_PYTHON=$(command -v python3 2>/dev/null)
fi
if [ ! -x "$REVIEW_PYTHON" ]; then
    echo "review-pipeline-cron: $REVIEW_PYTHON not executable" >&2
    exit 2
fi
exec 9>"$UA_HOME_DIR/review-pipeline.flock"
"$REVIEW_PYTHON" -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)'
lock_rc=$?
if [ "$lock_rc" -eq 75 ]; then
    echo "$(date -u +%FT%TZ) OK skipped — another review-pipeline-cron run is still in progress" >>"$LOG"
    exit 0
fi
if [ "$lock_rc" -ne 0 ]; then
    echo "review-pipeline-cron: could not acquire review lock (exit $lock_rc)" >&2
    exit 2
fi

export UA_WORKSPACE="${UA_WORKSPACE:-$HOME/dev/agent}"
# Keep UA_HOME explicit so the CLI and this wrapper agree on the store path.
export UA_HOME="$UA_HOME_DIR"

now=$(date -u +%FT%TZ)
today=$(date -u +%F)

cmd=("$REVIEW_BIN" scan --json)
if [ "${UA_REVIEW_DRY_RUN:-0}" = "1" ]; then
    cmd+=(--dry-run)
fi

out=$("${cmd[@]}" 2>&1)
rc=$?

have_jq=0
command -v jq >/dev/null 2>&1 && have_jq=1

# jq filter: normalize scan --json output (bare array, {results: [...]}, or
# {tasks: [...]}) to a flat array of result objects.
jq_items='(if type=="array" then . elif has("results") then .results elif has("tasks") then .tasks else [] end)'

level=""
summary=""

case "$rc" in
    0)
        if [ "$have_jq" -eq 1 ]; then
            total=$(printf '%s' "$out" | jq -e "$jq_items | length" 2>/dev/null)
            if [ -n "$total" ] && [ "$total" -eq 0 ] 2>/dev/null; then
                level="OK"
                summary="no tasks waiting for review"
            elif [ -n "$total" ]; then
                counts=$(printf '%s' "$out" | jq -r "$jq_items | group_by(.verdict) | map(\"\(.[0].verdict)=\(length)\") | join(\" \")" 2>/dev/null)
                level="EVENTS"
                summary="${counts:-review scan completed ($total candidates)}"
            else
                level="EVENTS"
                summary="review scan completed (unable to parse output)"
            fi
        else
            case "$out" in
                ""|*"[]"*)
                    level="OK"
                    summary="no tasks waiting for review"
                    ;;
                *)
                    level="EVENTS"
                    summary="review scan completed (jq unavailable for detail)"
                    ;;
            esac
        fi
        ;;
    1)
        if [ "$have_jq" -eq 1 ]; then
            red=$(printf '%s' "$out" | jq -r "[$jq_items[] | select(.verdict==\"red-tier\")] | length" 2>/dev/null)
            failed=$(printf '%s' "$out" | jq -r "[$jq_items[] | select(.verdict==\"failed\")] | length" 2>/dev/null)
            if [ -n "$red" ] || [ -n "$failed" ]; then
                level="FAIL"
                summary="red-tier=${red:-0} failed=${failed:-0}"
            else
                level="FAIL"
                summary="candidates failed or need review (unable to parse output)"
            fi
        else
            level="FAIL"
            summary="candidates failed or need review (jq unavailable for detail)"
        fi
        ;;
    *)
        level="FAIL"
        summary="config-error"
        ;;
esac

echo "$now $level $summary" >>"$LOG"

if [ "$rc" -eq 1 ] && [ -d "$INBOX" ]; then
    note="$INBOX/Review Pipeline $today.md"
    if [ ! -f "$note" ]; then
        printf -- '---\ndate: %s\nsource: review-pipeline-cron\ntags: [review, steward]\n---\n\n# Review pipeline findings — %s\n' \
            "$today" "$today" >"$note"
    fi
    printf -- '\n*Run at %s (scan --json, exit %s).*\n\n```\n%s\n```\n' "$now" "$rc" "$out" >>"$note"
fi

exit 0
