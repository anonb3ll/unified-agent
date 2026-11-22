#!/usr/bin/env bash
# Install every scripts/*-with-hub harness wrapper onto PATH (task #1362).
#
# The executor/dispatch defaults point at wrapper names, not raw CLIs:
# DEFAULT_CURSOR_BIN="agent-with-hub", DEFAULT_COPILOT_BIN="copilot-with-hub",
# DEFAULT_DEVIN_BIN="devin-with-hub". A machine missing one of these either
# fails the run (binary not found) or, if the operator points UA_*_BIN at the
# raw CLI, loses per-agent bearer isolation. The interactive launchers
# (cursor-with-hub, code-with-hub, github-copilot-desktop-with-hub,
# opencode-with-hub) give editors the same isolation.
#
# The wrappers hold no secrets: each reads its token file at runtime. This
# script copies files only; it does not mint, rotate or read tokens.
#
# Usage:
#   scripts/install-with-hub-wrappers.sh [--dry-run] [--dest DIR] [--only NAME]...
#
#   --dry-run    print what would be installed; change nothing
#   --dest DIR   install directory (default: $UA_WRAPPER_INSTALL_DIR or
#                $HOME/.local/bin)
#   --only NAME  install only this wrapper (repeatable)
#   --list       print the wrapper names and exit
#
# Idempotent: an up-to-date destination is left alone (mode is still fixed to
# 0755). Works with both GNU and BSD (macOS) coreutils.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
dest="${UA_WRAPPER_INSTALL_DIR:-$HOME/.local/bin}"
dry_run=0
list_only=0
only=()

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --list) list_only=1 ;;
    --dest)
      [[ $# -ge 2 ]] || { echo "--dest needs a directory" >&2; exit 2; }
      dest="$2"
      shift
      ;;
    --dest=*) dest="${1#--dest=}" ;;
    --only)
      [[ $# -ge 2 ]] || { echo "--only needs a wrapper name" >&2; exit 2; }
      only+=("$2")
      shift
      ;;
    --only=*) only+=("${1#--only=}") ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

wrappers=()
for src in "$SCRIPT_DIR"/*-with-hub; do
  [[ -f "$src" ]] || continue
  wrappers+=("$(basename "$src")")
done
if [[ ${#wrappers[@]} -eq 0 ]]; then
  echo "no *-with-hub wrappers found in $SCRIPT_DIR" >&2
  exit 1
fi

if [[ ${#only[@]} -gt 0 ]]; then
  for name in "${only[@]}"; do
    if [[ ! -f "$SCRIPT_DIR/$name" || "$name" != *-with-hub ]]; then
      echo "unknown wrapper: $name (known: ${wrappers[*]})" >&2
      exit 2
    fi
  done
  wrappers=("${only[@]}")
fi

if [[ $list_only -eq 1 ]]; then
  printf '%s\n' "${wrappers[@]}"
  exit 0
fi

if [[ $dry_run -eq 0 ]]; then
  mkdir -p "$dest"
fi

for name in "${wrappers[@]}"; do
  src="$SCRIPT_DIR/$name"
  target="$dest/$name"
  if [[ -f "$target" ]] && cmp -s "$src" "$target"; then
    if [[ $dry_run -eq 1 ]]; then
      echo "would keep $target (up to date)"
    else
      chmod 0755 "$target"
      echo "unchanged $target"
    fi
    continue
  fi
  if [[ $dry_run -eq 1 ]]; then
    echo "would install $src -> $target (mode 0755)"
  else
    install -m 0755 "$src" "$target"
    echo "installed $target"
  fi
done

if [[ $dry_run -eq 0 ]]; then
  case ":$PATH:" in
    *":$dest:"*) ;;
    *) echo "note: $dest is not on PATH; the executor defaults resolve wrappers via PATH" >&2 ;;
  esac
fi
