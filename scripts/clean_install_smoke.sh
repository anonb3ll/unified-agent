#!/usr/bin/env bash
# Clean-install smoke: install this checkout into a throwaway HOME, run
# `unified-agent init` as a stranger would, and require that:
#   1. `unified-agent doctor` reports no MISCONFIGURED/DEGRADED capability, and
#   2. the operator token init minted resolves to the `operator` profile.
#
# Nothing outside the temp directory is read or written: HOME, uv's tool dir
# and every UA_* variable are isolated. Tokens are never printed.
#
# Usage: scripts/clean_install_smoke.sh [operator name]
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="${1:-Pat Example}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Start from an environment with no unified-agent configuration at all.
while IFS='=' read -r key _; do
  case "$key" in UA_*) unset "$key" ;; esac
done < <(env)
export HOME="$tmp/home" UV_TOOL_DIR="$tmp/uv-tools" UV_TOOL_BIN_DIR="$tmp/bin"
mkdir -p "$HOME"

echo "== install"
uv tool install --quiet "$repo"
ua="$UV_TOOL_BIN_DIR/unified-agent"

echo "== init (operator: $name)"
"$ua" init --operator-name "$name" >/dev/null
"$ua" settings check

set -a
# shellcheck disable=SC1091
. "$HOME/.unified_agent/service.env"
set +a

echo "== doctor"
"$ua" doctor >"$tmp/doctor.txt" 2>&1 || {
  grep -E 'MISCONFIGURED|DEGRADED' "$tmp/doctor.txt" || cat "$tmp/doctor.txt"
  echo "FAIL: doctor reported a misconfiguration on a clean install" >&2
  exit 1
}
grep '^summary' "$tmp/doctor.txt"

echo "== operator token"
label="$("$ua" settings get operator.label)"
token="$(printf '%s' "$UA_AGENT_TOKENS" | tr ',' '\n' | awk -F: -v l="$label" '$1 == l { print $2 }')"
[ -n "$token" ] || { echo "FAIL: no token minted for $label" >&2; exit 1; }
profile="$("$ua" doctor --whoami --bearer "$token" | awk -F= '$1 == "profile" { print $2 }')"
[ "$profile" = "operator" ] || { echo "FAIL: $label resolved to profile '$profile'" >&2; exit 1; }
echo "$label -> operator"
echo "OK: clean install passed"
