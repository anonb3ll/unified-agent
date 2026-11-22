#!/usr/bin/env bash
# agy print-mode teardown canary (task #1172).
#
# Reproduces the live failure mode: agy -p backgrounds work, waits ~5s idle,
# prints "root agent idle; waiting up to 5s for N background task(s)", and may
# kill the background task — while the worktree write often still lands on disk.
#
# Run this on EVERY agy upgrade (cross-ref task #1143 / docs/automation.md) so an
# upstream fix is caught within minutes of bumping the CLI.
#
# Usage:
#   scripts/agy_teardown_canary.sh
#   scripts/agy_teardown_canary.sh --agy-bin /path/to/agy
#   scripts/agy_teardown_canary.sh --agy-bin ./fake-agy --workdir /tmp/canary-repo
#
# Exit codes:
#   0  SURVIVED — marker file written (background work completed or survived kill)
#   1  KILLED   — marker missing after the wait, and either the teardown text was
#                 seen (teardown=1) or, with no text, agy exited 0, its last
#                 stream-json `result` event says SUCCESS, and a run_command step
#                 whose CommandLine is exactly the canary launch command is DONE
#                 (silent=1, task #1791: agy 1.2.7+ drops the job without
#                 printing anything)
#   2  ERROR    — setup / agy invocation failure (nonzero agy exit, a missing or
#                 non-SUCCESS terminal result, or the canary command never ran)
#
# The marker wait (default 10 s, longer than the job's `sleep 8`) is set with
# UA_AGY_CANARY_MARKER_WAIT_S: whole seconds, at most five digits, read as
# decimal (`08` is 8). Tests set it to 0. The silent verdict parses the stream
# with python3; without python3 the run stays ERROR.
set -euo pipefail
export LC_ALL=C

AGY_BIN="${UA_AGY_BIN:-agy}"
WORKDIR=""
MARKER="canary_marker.txt"
KEEP_WORKDIR=0
MARKER_WAIT_S="${UA_AGY_CANARY_MARKER_WAIT_S:-10}"
if [[ ! "$MARKER_WAIT_S" =~ ^[0-9]{1,5}$ ]]; then
  echo "ERROR UA_AGY_CANARY_MARKER_WAIT_S must be a whole number of seconds (0-99999)" >&2
  exit 2
fi
# Force base 10: bash arithmetic reads a leading zero as octal, so `08` would
# be an invalid number and the wait would not run.
MARKER_WAIT_S=$((10#$MARKER_WAIT_S))

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agy-bin)
      AGY_BIN="${2:?}"
      shift 2
      ;;
    --workdir)
      WORKDIR="${2:?}"
      KEEP_WORKDIR=1
      shift 2
      ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed '$d'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$WORKDIR" ]]; then
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/agy-teardown-canary.XXXXXX")"
  cleanup() { rm -rf "$WORKDIR"; }
  trap cleanup EXIT
fi

mkdir -p "$WORKDIR"
cd "$WORKDIR"

if [[ ! -d .git ]]; then
  git init -q
  git config user.email "canary@unified-agent.example.com"
  git config user.name "agy-teardown-canary"
  printf 'seed\n' > README.canary
  git add README.canary
  git commit -q -m "canary seed"
fi

AGY_VERSION=""
if command -v "$AGY_BIN" >/dev/null 2>&1 || [[ -x "$AGY_BIN" ]]; then
  # `head -n1` closes its read end as soon as it has one line, which can send
  # SIGPIPE to a slower "$AGY_BIN" writer still mid-write. Under `pipefail`
  # that non-zero SIGPIPE exit makes the whole `cmd | head || fallback`
  # pipeline register as failed even though head already captured valid
  # output — so the old `|| echo unknown` fallback ALSO ran, and both writes
  # (head's real output AND the literal string "unknown") landed in this
  # variable joined by a newline. That embedded newline then split the
  # verdict line below into two lines under `echo`, silently pushing the
  # KILLED/SURVIVED prefix off of what callers assume is the last line —
  # this was the root cause of the flaky
  # test_canary_reports_killed_on_teardown_without_write CI failure.
  # `|| true` absorbs the SIGPIPE/exit status at the SOURCE instead, so the
  # pipe's own exit status (head's, always 0) is the only one pipefail sees.
  AGY_VERSION="$( ("$AGY_BIN" --version 2>/dev/null || true) | head -n1)"
fi
if [[ -z "$AGY_VERSION" ]]; then
  AGY_VERSION="unknown"
fi
# Belt-and-suspenders: never let a stray embedded newline (from this or any
# future AGY_VERSION source) split the verdict line printed below.
AGY_VERSION="${AGY_VERSION//$'\n'/ }"

# Fixture instruction: background a sleep+write so print-mode's idle drain can
# fire. Live agy may still write the marker before kill (SURVIVED) or not (KILLED).
# The silent-kill verdict below matches this exact command in the stream.
LAUNCH_COMMAND="(sleep 8; printf 'survived\\n' > ${MARKER}) &"
PROMPT=$(cat <<EOF
In this git repo, run EXACTLY this shell command in the background (do not wait
for it in the foreground):

  ${LAUNCH_COMMAND}

Then reply with the single word LAUNCHED and end your turn. Do not wait for the
background job.
EOF
)

OUT_FILE="$WORKDIR/agy.stdout"
ERR_FILE="$WORKDIR/agy.stderr"

# Blanket skip-permissions only with harness_sandbox's confirmed posture
# (task #1965). agy has no documented way to disable BypassSandbox, so
# require() refuses and this script exits 2. When a posture is verified, the
# sandbox argv it returns is placed before the flag.
SANDBOX_ARGV_FILE="$WORKDIR/sandbox.argv"
SANDBOX_ERR_FILE="$WORKDIR/sandbox.err"
# Load harness_sandbox by file path. Importing the dispatch package pulls the
# rest of the app (and its dependencies); this check only needs the chokepoint.
_CANARY_DIR="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_SRC="$(cd "$_CANARY_DIR/../src" && pwd)"
PY="$(command -v python3 || true)"
if [[ -z "$PY" || ! -f "$SANDBOX_SRC/unified_agent/dispatch/harness_sandbox.py" ]]; then
  echo "ERROR agy teardown canary refused: python3 or harness_sandbox.py is missing" >&2
  exit 2
fi
if ! "$PY" -c '
import importlib.util
import sys

path = sys.argv[1] + "/unified_agent/dispatch/harness_sandbox.py"
spec = importlib.util.spec_from_file_location("ua_harness_sandbox", path)
if spec is None or spec.loader is None:
    sys.stderr.write("harness_sandbox module is missing\n")
    raise SystemExit(1)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
posture = module.resolve("agy")
try:
    argv = module.require("agy", posture, flags="--dangerously-skip-permissions")
except module.HarnessSandboxUnavailable as exc:
    sys.stderr.write(str(exc) + "\n")
    raise SystemExit(1)
sys.stdout.buffer.write(b"\0".join(part.encode() for part in argv))
' "$SANDBOX_SRC" >"$SANDBOX_ARGV_FILE" 2>"$SANDBOX_ERR_FILE"; then
  echo "ERROR agy teardown canary refused: sandbox posture is not verified" >&2
  cat "$SANDBOX_ERR_FILE" >&2
  exit 2
fi
SANDBOX_ARGV=()
while IFS= read -r -d '' token || [[ -n "$token" ]]; do
  SANDBOX_ARGV+=("$token")
done <"$SANDBOX_ARGV_FILE"
if [[ ${#SANDBOX_ARGV[@]} -eq 0 ]]; then
  echo "ERROR agy teardown canary refused: sandbox posture returned no sandbox argv" >&2
  exit 2
fi

set +e
"$AGY_BIN" -p "$PROMPT" --output-format stream-json \
  "${SANDBOX_ARGV[@]}" \
  --dangerously-skip-permissions \
  >"$OUT_FILE" 2>"$ERR_FILE"
AGY_RC=$?
set -e

BLOB="$(cat "$OUT_FILE" "$ERR_FILE" 2>/dev/null || true)"
TEARDOWN=0
if printf '%s' "$BLOB" | grep -Eiq \
  'root agent idle;\s*waiting up to|terminating[[:space:]]+[0-9]+[[:space:]]+background task|Print mode:[[:space:]]*drain grace period expired|All your background tasks have been stopped'; then
  TEARDOWN=1
fi

# The backgrounded job sleeps 8 s, so a job that survived agy's exit may land
# its marker after agy returns. Wait for it before calling the run lost.
for ((waited = 0; waited < MARKER_WAIT_S; waited++)); do
  [[ -f "$MARKER" ]] && break
  sleep 1
done

if [[ -f "$MARKER" ]]; then
  echo "SURVIVED agy=${AGY_VERSION} teardown=${TEARDOWN} exit=${AGY_RC} marker=${MARKER}"
  exit 0
fi

if [[ "$TEARDOWN" -eq 1 ]]; then
  echo "KILLED agy=${AGY_VERSION} teardown=1 exit=${AGY_RC} marker_missing=1"
  exit 1
fi

# Silent loss (task #1791): agy 1.2.7+ prints no teardown text. The run is a
# silent kill only when all of these hold: agy exited 0; the last stream-json
# `result` event reports status SUCCESS (an agy-reported failure is an
# invocation ERROR, not a kill); and a run_command step whose CommandLine is
# exactly the canary launch command (whitespace-normalized) finished DONE. A
# command that merely mentions the marker file does not count. agy's stream
# carries no exit status for a run_command step, so the exact-command match is
# the launch evidence. Anything else stays ERROR.
#
# Prints "<last result status|missing> <1 if the launch step is DONE, else 0>".
stream_facts() {
  python3 -I - "$1" "$2" <<'PY'
import json
import sys

path, launch = sys.argv[1], " ".join(sys.argv[2].split())
status, launched = "missing", 0
with open(path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "result":
            result = event.get("result")
            value = result.get("status") if isinstance(result, dict) else None
            status = "".join(value.split()) if isinstance(value, str) else ""
            status = status or "unknown"
            continue
        step = event.get("step_update")
        if event.get("event") != "step_update" or not isinstance(step, dict):
            continue
        if step.get("step_type") != "tool" or step.get("state") != "DONE":
            continue
        if step.get("tool_name") != "run_command":
            continue
        info = step.get("tool_info")
        params = info.get("parameters") if isinstance(info, dict) else None
        if not isinstance(params, dict):
            continue
        command = params.get("CommandLine") or params.get("command") or ""
        if isinstance(command, str) and " ".join(command.split()) == launch:
            launched = 1
print(status, launched)
PY
}

STREAM_FACTS="unparsed 0"
if command -v python3 >/dev/null 2>&1; then
  STREAM_FACTS="$(stream_facts "$OUT_FILE" "$LAUNCH_COMMAND" 2>/dev/null)" || STREAM_FACTS="unparsed 0"
fi
read -r RESULT_STATUS CANARY_DONE <<<"$STREAM_FACTS"
if [[ "$AGY_RC" -eq 0 && "$RESULT_STATUS" == "SUCCESS" && "$CANARY_DONE" == "1" ]]; then
  echo "KILLED agy=${AGY_VERSION} teardown=0 silent=1 exit=${AGY_RC} marker_missing=1"
  exit 1
fi

echo "ERROR agy=${AGY_VERSION} exit=${AGY_RC} result=${RESULT_STATUS} canary_done=${CANARY_DONE:-0} no_marker no_teardown_signature" >&2
if [[ "$KEEP_WORKDIR" -eq 1 ]]; then
  echo "stdout: $OUT_FILE stderr: $ERR_FILE" >&2
fi
exit 2
