#!/usr/bin/env bash
# node-release-roll.sh: roll this host's ua-node to a release worktree (task #2298).
#
# A node supervisor (systemd ua-node on Linux, LaunchAgent com.unified-agent.node
# on macOS) must name only the stable link ~/.unified_agent/release/current. A
# roll builds a new release worktree, proves it imports from itself, swaps the
# link atomically, restarts the node, and swaps back if the node does not come
# up. Once the node is on the new release, the host's idle executors restart onto
# it too (task #2354). Docs: docs/deploy/node-release-roll.md.
#
#   scripts/node-release-roll.sh --sha <sha> [--dry-run]   roll to <sha>
#   scripts/node-release-roll.sh --check-only              report, change nothing
#   scripts/node-release-roll.sh --retire-stale [--yes]    list (remove) old releases
#
# Options: --repo <path> (any checkout of the repo; default: the one holding this
# script). Environment: UA_ROLL_UNIT (default ua-node), UA_ROLL_LABEL (default
# com.unified-agent.node), UA_ROLL_WAIT_SECONDS (default 30),
# UA_ROLL_SETTLE_SECONDS (default 15; longer than the unit's RestartSec, so a
# crash loop is caught), UA_ROLL_TMUX_BASE (default /tmp; holds agy's tmux sockets).
#
# The gate never runs `unified-agent --version`: on a hub-client host it exits 1
# with LiveStoreError by design (task #2297).
#
# Exit status of a roll: 0 complete; 1 failed (the node is back on its old release
# unless the message says otherwise); 3 the node roll is complete and stays, but an
# executor is not on the new release. Re-run the same command to finish it.
set -euo pipefail
unset CDPATH

UNIT="${UA_ROLL_UNIT:-ua-node}"
LABEL="${UA_ROLL_LABEL:-com.unified-agent.node}"
WAIT="${UA_ROLL_WAIT_SECONDS:-30}"
SETTLE="${UA_ROLL_SETTLE_SECONDS:-15}"
RELEASE_DIR="$HOME/.unified_agent/release"
LINK="$RELEASE_DIR/current"
PREV_LINK="$RELEASE_DIR/previous"
# One lock serializes rolls and retirements on this host.
LOCK="$RELEASE_DIR/.roll.lock"
# Recovery record for a roll in flight: symlinks `prev` and `next`. Written
# before the swap and removed only once the roll is complete or undone, so a
# killed roll is undone by the next run.
TXN="$RELEASE_DIR/.roll.txn"
EXE_SUFFIX="/.venv/bin/unified-agent"
DOC="docs/deploy/node-release-roll.md"
# Where executor/agy_interactive.py puts its private tmux servers (_tmux_socket_base).
TMUX_BASE="${UA_ROLL_TMUX_BASE:-/tmp}"
# The node rolled and stays, but an executor did not move.
EXIT_EXECUTORS=3

# Pipelines in this script never let a reader stop early: under pipefail a
# writer killed by SIGPIPE fails the whole run with status 141 and no message.
# Command output is captured into a variable first and matched in bash.

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  echo "node-release-roll: $*" >&2
  exit 1
}

warn() { echo "node-release-roll: warning: $*" >&2; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
sha=""
mode=""
dry_run=0
yes=0

set_mode() {
  [ -z "$mode" ] || [ "$mode" = "$1" ] || die "choose one of --sha, --check-only, --retire-stale"
  mode="$1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --sha)
      [ $# -ge 2 ] || die "--sha needs a commit"
      sha="$2"
      set_mode roll
      shift 2
      ;;
    --repo)
      [ $# -ge 2 ] || die "--repo needs a path"
      repo="$2"
      shift 2
      ;;
    --check-only) set_mode check; shift ;;
    --retire-stale) set_mode retire; shift ;;
    --dry-run) dry_run=1; shift ;;
    --yes) yes=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "node-release-roll: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$mode" ] || { usage >&2; exit 2; }
[ "$yes" -eq 0 ] || [ "$mode" = retire ] || die "--yes applies only to --retire-stale"
case "$WAIT$SETTLE" in *[!0-9]*) die "UA_ROLL_WAIT_SECONDS and UA_ROLL_SETTLE_SECONDS must be integers" ;; esac

case "$(uname -s)" in
  Linux) platform=linux ;;
  Darwin) platform=macos ;;
  *) die "unsupported platform: $(uname -s)" ;;
esac
uid="$(id -u)"

# Executors this platform may host: systemd units or launchd labels.
if [ "$platform" = linux ]; then
  EXECUTORS=(unified-agent-executor.service unified-agent-executor-codex.service)
else
  EXECUTORS=(com.unified-agent.executor-agy com.unified-agent.executor-codex)
fi

# Every `worktree <path>` of the repo, one per line. The first is the main checkout.
worktree_paths() {
  local listing
  listing="$(git -C "$1" worktree list --porcelain 2>/dev/null)" || return 1
  awk '/^worktree /{print substr($0, 10)}' <<<"$listing"
}

# The main checkout, even when this script runs from inside a release worktree:
# new release worktrees go beside the others, never nested in one (task #187).
all_paths="$(worktree_paths "$repo")" || die "not a git checkout: $repo"
main_root="${all_paths%%$'\n'*}"
[ -n "$main_root" ] || die "not a git checkout: $repo"

phys() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s\n' "$1"; }

# A symlink's target as an absolute path, resolved against the link's own
# directory (not the caller's cwd), physical when it exists. Empty: no link.
link_target() {
  local raw
  raw="$(readlink "$1" 2>/dev/null)" || return 0
  case "$raw" in /*) ;; *) raw="$(dirname "$1")/$raw" ;; esac
  phys "$raw"
}

say() { echo "==> $*"; }

# Run a command, or print it under --dry-run.
run() {
  if [ "$dry_run" -eq 1 ]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

# Point <link> at <target> with one rename(2): a reader sees the old target or
# the new one, never a missing link.
swap_link() {
  local target="$1" link="$2" tmp
  if [ "$dry_run" -eq 1 ]; then
    echo "[dry-run] ln -sfn $target $link.tmp.<pid> && mv (atomic rename) -> $link"
    return 0
  fi
  tmp="$link.tmp.$$"
  ln -sfn "$target" "$tmp"
  if mv --version >/dev/null 2>&1; then
    mv -Tf "$tmp" "$link" # GNU
  else
    mv -fh "$tmp" "$link" # BSD/macOS: -h replaces the link, not its target
  fi
}

# --- lock ---------------------------------------------------------------------

lock_held=0

release_lock() {
  [ "$lock_held" -eq 1 ] || return 0
  rm -f "$LOCK/pid"
  rmdir "$LOCK" 2>/dev/null || true
  lock_held=0
}

# Taken before any link is read or worktree prepared. A lock whose pid is dead
# (a SIGKILLed run) is reclaimed; a live one refuses.
acquire_lock() {
  [ "$dry_run" -eq 0 ] || return 0
  mkdir -p "$RELEASE_DIR"
  local holder
  if ! mkdir "$LOCK" 2>/dev/null; then
    holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
      warn "reclaiming the lock of dead roll pid $holder"
      rm -f "$LOCK/pid"
      rmdir "$LOCK" 2>/dev/null || true
    fi
    mkdir "$LOCK" 2>/dev/null ||
      die "another roll or retirement holds $LOCK (pid ${holder:-unknown}); remove it only if none is running"
  fi
  echo "$$" >"$LOCK/pid"
  lock_held=1
  trap release_lock EXIT
  trap on_signal INT TERM HUP
}

# --- supervisor and service ---------------------------------------------------

# The supervisor's effective program path, not the unit file (`systemctl cat`
# showed a templated path while a drop-in named another).
supervisor_program() {
  local out line
  if [ "$platform" = linux ]; then
    out="$(systemctl --user show -p ExecStart --value "$UNIT" 2>/dev/null || true)"
    # { path=/x/.venv/bin/unified-agent ; argv[]=/x/... node ; ... }
    [[ $out == *"path="* ]] || return 0
    out="${out#*path=}"
    printf '%s\n' "${out%% ;*}"
    return 0
  fi
  out="$(launchctl print "gui/$uid/$LABEL" 2>/dev/null || true)"
  while IFS= read -r line; do
    if [[ $line =~ ^[[:space:]]*program\ =\ (.*)$ ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  done <<<"$out"
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  [ -f "$plist" ] || return 0
  if command -v plutil >/dev/null 2>&1; then
    plutil -extract ProgramArguments.0 raw -o - "$plist" 2>/dev/null || true
  else
    awk '/<key>ProgramArguments<\/key>/{f = 1; next}
      f && /<string>/ && !done {sub(/.*<string>/, ""); sub(/<\/string>.*/, ""); print; done = 1}' "$plist"
  fi
}

# True only for this host's exact link, never another home's release/current.
supervisor_names_link() {
  local exe
  exe="$(supervisor_program)"
  [ -n "$exe" ] || return 1
  [ "$exe" = "$LINK$EXE_SUFFIX" ] ||
    [ "$exe" = "$(phys "$HOME")/.unified_agent/release/current$EXE_SUFFIX" ]
}

# The tree the supervisor runs, resolved through any link it names.
supervisor_tree() {
  local exe
  exe="$(supervisor_program)"
  [[ $exe == *"$EXE_SUFFIX" ]] || return 0
  phys "${exe%"$EXE_SUFFIX"}"
}

# Main pid of the node, or of the service named by $1.
service_pid() {
  local out unit="${1:-$UNIT}" label="${1:-$LABEL}"
  if [ "$platform" = linux ]; then
    systemctl --user is-active --quiet "$unit" || return 1
    systemctl --user show -p MainPID --value "$unit" 2>/dev/null || true
    return 0
  fi
  out="$(launchctl print "gui/$uid/$label" 2>/dev/null)" || return 1
  [[ $out == *"state = running"* ]] || return 1
  if [[ $out =~ pid\ =\ ([0-9]+) ]]; then echo "${BASH_REMATCH[1]}"; fi
}

# Restart the node, or the service named by $1.
restart_service() {
  local unit="${1:-$UNIT}" label="${1:-$LABEL}"
  if [ "$platform" = linux ]; then
    run systemctl --user restart "$unit"
  elif ! run launchctl kickstart -k "gui/$uid/$label"; then
    echo "node-release-roll: launchctl kickstart failed. Run this from a normal" \
      "login shell: launchd refuses it inside a supervised Hermes gateway." >&2
    return 1
  fi
}

# Up within WAIT seconds, and still up on the same pid SETTLE seconds later.
wait_healthy() {
  local i pid1="" pid2
  [ "$dry_run" -eq 0 ] || { echo "[dry-run] wait up to ${WAIT}s for $UNIT, then ${SETTLE}s settle"; return 0; }
  for ((i = 0; i <= WAIT; i++)); do
    if pid1="$(service_pid)"; then break; fi
    pid1=""
    if [ "$i" -lt "$WAIT" ]; then sleep 1; fi
  done
  [ -n "$pid1" ] || { warn "node not running after ${WAIT}s"; return 1; }
  [ "$SETTLE" -eq 0 ] || sleep "$SETTLE"
  pid2="$(service_pid)" || { warn "node stopped within ${SETTLE}s of starting"; return 1; }
  [ "$pid1" = "$pid2" ] || { warn "node restarted within ${SETTLE}s (pid $pid1 -> $pid2)"; return 1; }
}

# --- transaction --------------------------------------------------------------

swapped=0
txn_prev=""
# Set once the node roll is final: from here a signal must not report or undo it.
node_done=0

begin_txn() {
  local tmp="$TXN.tmp.$$"
  [ "$dry_run" -eq 0 ] || return 0
  [ ! -e "$TXN" ] || die "$TXN already exists"
  mkdir "$tmp"
  [ -z "$1" ] || ln -s "$1" "$tmp/prev"
  ln -s "$2" "$tmp/next"
  mv "$tmp" "$TXN"
}

end_txn() {
  [ "$dry_run" -eq 0 ] || return 0
  [ -d "$TXN" ] || return 0
  rm -f "$TXN/prev" "$TXN/next"
  rmdir "$TXN"
}

# Swap back to <prev> and restart. Always ends the run non-zero.
roll_back() {
  local prev="$1"
  if [ -z "$prev" ]; then
    run rm -f "$LINK"
    swapped=0
    end_txn
    die "roll failed and there was no previous target; link removed, node left stopped"
  fi
  warn "roll failed; pointing $LINK back at $prev"
  swap_link "$prev" "$LINK"
  swapped=0
  end_txn
  if restart_service && wait_healthy; then
    die "rolled back to $prev (node healthy)"
  fi
  die "rolled back to $prev, but the node is NOT healthy; see $DOC"
}

on_signal() {
  trap - INT TERM HUP
  if [ "$node_done" -eq 1 ]; then
    echo "node-release-roll: interrupted during the executor restarts; the node roll" \
      "is complete and stays. Re-run the same command to finish the executors." >&2
    exit "$EXIT_EXECUTORS"
  fi
  if [ "$swapped" -eq 1 ]; then
    warn "interrupted after the swap"
    roll_back "$txn_prev"
  fi
  end_txn
  die "interrupted; nothing switched"
}

# A previous run died between the swap and its end: undo it first.
recover_txn() {
  [ -d "$TXN" ] || return 0
  local prev
  prev="$(link_target "$TXN/prev")"
  warn "an interrupted roll left $TXN; restoring $LINK -> ${prev:-<none>}"
  if [ -z "$prev" ]; then
    run rm -f "$LINK"
    end_txn
    return 0
  fi
  swap_link "$prev" "$LINK"
  end_txn
  if ! restart_service || ! wait_healthy; then
    die "restored $LINK -> $prev after an interrupted roll, but the node is NOT healthy"
  fi
  echo "recovered the interrupted roll: $LINK -> $prev"
}

# --- executors ----------------------------------------------------------------

executor_installed() {
  local state
  if [ "$platform" = linux ]; then
    state="$(systemctl --user show -p LoadState --value "$1" 2>/dev/null || true)"
    [ "$state" = loaded ]
    return
  fi
  launchctl print "gui/$uid/$1" >/dev/null 2>&1
}

process_command() { ps -o command= -p "$1" 2>/dev/null || true; }

# Direct children of pid $1, space separated. Fails when ps does: unknown is not
# idle. One keyword per -o: BSD ps reads everything after `=` as a column header.
child_pids() {
  local out
  out="$(ps -axo ppid= -o pid= 2>/dev/null)" || return 1
  [ -n "$out" ] || return 1
  awk -v p="$1" '$1 == p {printf "%s ", $2}' <<<"$out"
}

# Live tmux sessions of executor pid $1, space separated. A tmux-backed agy run
# has no direct child of the executor: its private server daemonizes away. Each
# run gets <base>/ua-agy-i-*/tmux.sock and a session ua-agy-i-<executor pid>-<ms>.
# Fails when that cannot be read: unknown is not idle. A server that refuses the
# connection is dead (a killed executor leaves its socket behind), not unknown.
tmux_sessions_of() {
  local sock out name found=""
  [ -d "$TMUX_BASE" ] && [ -w "$TMUX_BASE" ] && [ -x "$TMUX_BASE" ] || return 1
  for sock in "$TMUX_BASE"/ua-agy-i-*/tmux.sock; do
    [ -S "$sock" ] || continue
    command -v tmux >/dev/null 2>&1 || return 1
    if ! out="$(tmux -S "$sock" list-sessions -F '#{session_name}' 2>&1)"; then
      [[ $out == *"no server running"* ]] || [ ! -S "$sock" ] || return 1
      continue
    fi
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      [[ $name =~ ^ua-agy-i-([0-9]+)-[0-9]+$ ]] || return 1
      if [ "${BASH_REMATCH[1]}" = "$1" ]; then found="$found$name "; fi
    done <<<"$out"
  done
  printf '%s' "$found"
}

# What a command line says about release $2. Every path into a .venv/bin must
# lead to it. Prints:
#   direct   at least one such path names the release directory itself
#   link     only release/current appears, and it resolves to the release. This
#            proves the release only for a process started after the swap.
#   other    some path leads elsewhere, or no such path appears
release_named_by() {
  local wt="$2" word tree via="" words=()
  read -r -a words <<<"$1"
  [ "${#words[@]}" -gt 0 ] || { echo other; return 0; }
  for word in "${words[@]}"; do
    [[ $word == /* && $word == */.venv/bin/* ]] || continue
    tree="${word%%/.venv/bin/*}"
    [ "$(phys "$tree")" = "$wt" ] || { echo other; return 0; }
    if [ "$tree" = "$LINK" ] || [ "$tree" = "$(phys "$HOME")/.unified_agent/release/current" ]; then
      [ -n "$via" ] || via="link"
    else
      via=direct
    fi
  done
  echo "${via:-other}"
}

# After a restart: a pid other than $2 whose command line names release $3, still
# the same pid SETTLE seconds later. Sets verified_via. Not in a command
# substitution, so a signal is handled between polls.
verified_via=""
wait_executor() {
  local name="$1" old_pid="$2" wt="$3" i pid="" cmd="" via=other later
  for ((i = 0; i <= WAIT; i++)); do
    pid="$(service_pid "$name" || true)"
    if [ -n "$pid" ] && [ "$pid" != 0 ] && [ "$pid" != "$old_pid" ]; then
      cmd="$(process_command "$pid")"
      via="$(release_named_by "$cmd" "$wt")"
      [ "$via" = other ] || break
    fi
    if [ "$i" -lt "$WAIT" ]; then sleep 1; fi
  done
  if [ "$via" = other ]; then
    if [ -z "$pid" ] || [ "$pid" = 0 ]; then
      warn "executor $name: not running ${WAIT}s after its restart"
    elif [ "$pid" = "$old_pid" ]; then
      warn "executor $name: still pid $pid ${WAIT}s after its restart"
    else
      warn "executor $name: pid $pid does not run $wt; command line: ${cmd:-(empty)}"
    fi
    return 1
  fi
  [ "$SETTLE" -eq 0 ] || sleep "$SETTLE"
  later="$(service_pid "$name" || true)"
  [ "$later" = "$pid" ] || {
    warn "executor $name: pid $pid did not stay up for ${SETTLE}s (now: ${later:-none})"
    return 1
  }
  verified_via="$via"
}

# Move this host's executors to release $1 (commit $2). Runs after the node roll
# is final and under its lock, so the link cannot move during the checks. Nothing
# here may undo the roll. Each executor is handled on its own; any that does not
# end up on the release is named, and the run exits EXIT_EXECUTORS.
roll_executors() {
  local wt="$1" full="$2" sha8="${2:0:8}" name pid cmd kids sessions failed=""
  node_done=1
  for name in "${EXECUTORS[@]}"; do
    if ! executor_installed "$name"; then
      echo "executor $name: not installed on this host"
      continue
    fi
    pid="$(service_pid "$name" || true)"
    if [ -z "$pid" ] || [ "$pid" = 0 ]; then
      # Stopped on purpose or between crashes; a restart here would start it.
      echo "executor $name: installed but not running; left stopped"
      continue
    fi
    # Only a command line that names the directory proves this. One showing just
    # the link says nothing about a process that may predate the swap.
    cmd="$(process_command "$pid")"
    if [ "$(release_named_by "$cmd" "$wt")" = direct ]; then
      echo "executor $name: already on $sha8 (pid $pid)"
      continue
    fi
    if ! kids="$(child_pids "$pid")"; then
      warn "executor $name (pid $pid): could not list its child processes; treated as BUSY, NOT restarted"
      failed="$failed $name"
      continue
    fi
    if [ -n "$kids" ]; then
      warn "executor $name is BUSY (pid $pid, child pids: ${kids% }); NOT restarted, still on its old release"
      failed="$failed $name"
      continue
    fi
    if ! sessions="$(tmux_sessions_of "$pid")"; then
      warn "executor $name (pid $pid): could not read agy tmux sessions under $TMUX_BASE; treated as BUSY, NOT restarted"
      failed="$failed $name"
      continue
    fi
    if [ -n "$sessions" ]; then
      warn "executor $name is BUSY (pid $pid, tmux sessions: ${sessions% }); NOT restarted, still on its old release"
      failed="$failed $name"
      continue
    fi
    if ! restart_service "$name"; then
      warn "executor $name: restart failed"
      failed="$failed $name"
      continue
    fi
    if [ "$dry_run" -eq 1 ]; then
      echo "[dry-run] wait up to ${WAIT}s for a new $name pid whose command line names $wt, then ${SETTLE}s settle"
      continue
    fi
    if ! wait_executor "$name" "$pid" "$wt"; then
      failed="$failed $name"
      continue
    fi
    if [ "$verified_via" = direct ]; then
      echo "executor $name: on $sha8 (command line names $wt)"
    else
      echo "executor $name: on $sha8 (command line shows release/current, which resolves to $wt; new pid since the swap)"
    fi
  done
  if [ -z "$failed" ] || [ "$dry_run" -eq 1 ]; then return 0; fi
  warn "the node roll to $sha8 is complete and stays, but these executors are not on it:$failed"
  warn "when they are idle, re-run: scripts/node-release-roll.sh --sha $full"
  return "$EXIT_EXECUTORS"
}

# --- inventory ----------------------------------------------------------------

# Release worktrees of this repo, one path per line.
release_worktrees() {
  local paths wt
  paths="$(worktree_paths "$main_root")" || die "git worktree list failed in $main_root"
  while IFS= read -r wt; do
    [[ $wt == */release-* && ${wt##*/} == release-* ]] && printf '%s\n' "$wt"
  done <<<"$paths"
  return 0
}

# Read once per run, after the lock: what must never be retired.
cur=""
prev_t=""
sup_exe=""
sup_tree=""
procs=""
load_state() {
  cur="$(link_target "$LINK")"
  prev_t="$(link_target "$PREV_LINK")"
  sup_exe="$(supervisor_program)"
  sup_tree="$(supervisor_tree)"
  procs="$(ps -axo command 2>/dev/null || true)"
}

classify() {
  local wt="$1" w
  w="$(phys "$wt")"
  if [ -n "$cur" ] && [ "$w" = "$cur" ]; then
    echo current
  elif [ -n "$prev_t" ] && [ "$w" = "$prev_t" ]; then
    echo previous
  elif [ -n "$sup_tree" ] && [ "$w" = "$sup_tree" ]; then
    echo pinned
  elif [ -n "$sup_exe" ] && [[ $sup_exe == "$wt/"* || $sup_exe == "$w/"* ]]; then
    echo pinned
  elif [[ $procs == *"$wt/"* || $procs == *"$w/"* ]]; then
    echo in-use
  else
    echo stale
  fi
}

# --- modes --------------------------------------------------------------------

do_check() {
  load_state
  echo "platform: $platform"
  echo "repo: $main_root"
  echo "link: $LINK"
  if [ -z "$cur" ]; then
    echo "target: (no link)"
  elif [ -d "$cur" ]; then
    echo "target: $cur (exists)"
  else
    echo "target: $cur (MISSING: the node cannot start from it)"
  fi
  if [ -n "$prev_t" ]; then
    if [ -d "$prev_t" ]; then echo "previous: $prev_t (exists)"; else echo "previous: $prev_t (MISSING)"; fi
  else
    echo "previous: (none)"
  fi
  if [ -d "$TXN" ]; then
    echo "interrupted roll: yes (the next roll restores $(link_target "$TXN/prev"))"
  fi
  if supervisor_names_link; then
    echo "supervisor names release/current: yes"
  else
    echo "supervisor names release/current: no"
    echo "  effective: ${sup_exe:-(unknown)}"
    echo "  expected:  $LINK$EXE_SUFFIX"
    echo "  fix: install the drop-in in $DOC"
  fi
  if service_pid >/dev/null; then echo "node running: yes"; else echo "node running: no"; fi
  echo "release worktrees:"
  local wts wt
  wts="$(release_worktrees)"
  while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    printf '  %-8s %s\n' "$(classify "$wt")" "$wt"
  done <<<"$wts"
}

do_retire() {
  local wts wt failed=0 found=0
  [ "$yes" -eq 0 ] || acquire_lock
  [ ! -d "$TXN" ] || die "an interrupted roll is pending ($TXN); run the roll again first"
  load_state
  if [ -z "$cur" ] || [ ! -d "$cur" ]; then
    die "no live release link at $LINK; refusing to retire anything"
  fi
  supervisor_names_link ||
    die "the $UNIT supervisor does not run $LINK$EXE_SUFFIX; a versioned worktree may be live. See $DOC"
  wts="$(release_worktrees)"
  while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    [ "$(classify "$wt")" = stale ] || continue
    found=1
    if [ "$yes" -eq 1 ]; then
      run git -C "$main_root" worktree remove "$wt" || { warn "kept $wt (git refused)"; failed=1; }
    else
      echo "stale: $wt"
    fi
  done <<<"$wts"
  [ "$found" -eq 1 ] || echo "no stale release worktrees"
  [ "$found" -eq 0 ] || [ "$yes" -eq 1 ] || echo "re-run with --yes to remove them (git worktree remove, never forced)"
  return "$failed"
}

gate() {
  local wt="$1" file version py="$1/.venv/bin/python"
  if [ "$dry_run" -eq 1 ]; then
    echo "[dry-run] gate: $py imports unified_agent from $wt and reports its version"
    return 0
  fi
  [ -x "$py" ] || { warn "gate: $py missing"; return 1; }
  file="$(cd / && "$py" -I -c 'import unified_agent; print(unified_agent.__file__)')" ||
    { warn "gate: import unified_agent failed"; return 1; }
  case "$file" in
    "$wt"/*) ;;
    *) warn "gate: unified_agent imports from $file, not $wt"; return 1 ;;
  esac
  version="$(cd / && "$py" -I -c "import importlib.metadata as m; print(m.version('unified-agent'))")" ||
    { warn "gate: unified-agent package metadata missing"; return 1; }
  echo "gate: ok (unified-agent $version from $file)"
}

do_roll() {
  local full sha8 wt="" name prev wts candidate dirty
  if [[ ! $sha =~ ^[0-9a-fA-F]{40}$ && ! $sha =~ ^[0-9a-fA-F]{64}$ ]]; then
    die "--sha must be the full 40- or 64-character commit id, not a branch, tag, or other revision"
  fi
  acquire_lock
  full="$(git -C "$main_root" rev-parse --verify --quiet "${sha}^{commit}")" ||
    die "commit $sha not found in $main_root (git fetch first)"
  sha8="${full:0:8}"

  [ ! -e "$LINK" ] || [ -L "$LINK" ] || die "$LINK exists and is not a symlink"
  supervisor_names_link ||
    die "the $UNIT supervisor does not run $LINK$EXE_SUFFIX; a swap would change nothing. Install the drop-in in $DOC first"
  recover_txn
  prev="$(link_target "$LINK")"
  [ -z "$prev" ] || [ -d "$prev" ] || warn "current target $prev is missing; there is no rollback"

  wts="$(release_worktrees)"
  while IFS= read -r candidate; do
    if [[ ${candidate##*/} == release-*-"$sha8" ]]; then
      wt="$(phys "$candidate")"
      break
    fi
  done <<<"$wts"

  if [ -n "$wt" ]; then
    [ "$(git -C "$wt" rev-parse HEAD)" = "$full" ] || die "$wt is not at $full"
    dirty="$(git -C "$wt" status --porcelain --untracked-files=no)" || die "git status failed in $wt"
    [ -z "$dirty" ] || die "$wt has uncommitted changes to tracked files; refusing to reuse it"
    if [ -n "$prev" ] && [ "$prev" = "$wt" ]; then
      # Complete only once the node is healthy on it.
      if [ "$dry_run" -eq 1 ] || wait_healthy; then
        echo "already on $sha8 ($wt) and healthy; node untouched"
        roll_executors "$wt" "$full"
        return 0
      fi
      warn "already on $sha8 but the node is not healthy; syncing and restarting"
      if [ "$dry_run" -eq 1 ]; then
        echo "[dry-run] (cd $wt && uv sync --all-extras)"
      else
        (cd "$wt" && uv sync --all-extras) || die "uv sync failed in $wt"
      fi
      gate "$wt" || die "gate failed for $wt"
      if ! restart_service || ! wait_healthy; then
        die "on $sha8 ($wt) but the node is NOT healthy; see $DOC"
      fi
      echo "on $sha8: node restarted and healthy"
      roll_executors "$wt" "$full"
      return 0
    fi
    say "1/7 reuse $wt"
  else
    name="release-$(date +%Y%m%d)-$sha8"
    wt="$main_root/.worktrees/$name"
    say "1/7 git worktree add $wt"
    run git -C "$main_root" worktree add --detach "$wt" "$full"
    [ "$dry_run" -eq 1 ] || wt="$(phys "$wt")"
  fi

  say "2/7 uv sync --all-extras"
  if [ "$dry_run" -eq 1 ]; then
    echo "[dry-run] (cd $wt && uv sync --all-extras)"
  else
    (cd "$wt" && uv sync --all-extras) || die "uv sync failed in $wt; nothing switched"
  fi

  say "3/7 pre-swap gate"
  gate "$wt" || die "gate failed for $wt; nothing switched"

  say "4/7 swap $LINK -> $wt (was: ${prev:-none})"
  txn_prev="$prev"
  begin_txn "$prev" "$wt"
  swapped=1
  swap_link "$wt" "$LINK"

  say "5/7 restart $UNIT"
  if ! restart_service || ! wait_healthy; then
    roll_back "$prev"
  fi

  say "6/7 verify"
  if [ "$dry_run" -eq 0 ] && [ "$(link_target "$LINK")" != "$wt" ]; then
    roll_back "$prev"
  fi
  [ -z "$prev" ] || swap_link "$prev" "$PREV_LINK"
  swapped=0
  end_txn
  echo "rolled to $sha8: $LINK -> $wt"

  say "7/7 restart idle executors onto $sha8"
  roll_executors "$wt" "$full"
}

case "$mode" in
  check) do_check ;;
  retire) do_retire ;;
  roll) do_roll ;;
esac
