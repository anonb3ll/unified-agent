#!/usr/bin/env bash
# S0 smoke test against a fake hub. Needs a display (use `xvfb-run -a
# dbus-run-session -- scripts/smoke.sh` headless) and a built debug binary.
#
# Proves: the offline page recovers to /ui once the hub answers; --toggle and
# deep links are forwarded to the running instance; object-path links are
# refused; and a hostile /ui can call no native command (decision 4).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${BIN:-$HERE/target/debug/unified-agent-desktop}"
PORT="${PORT:-8799}"
WORK="$(mktemp -d)"
trap 'kill ${APP:-} ${SRV:-} 2>/dev/null || true; rm -rf "$WORK"' EXIT
export XDG_CONFIG_HOME="$WORK/config" XDG_DATA_HOME="$WORK/data"

mkdir -p "$WORK/hub/ui"
echo ok > "$WORK/hub/healthz"
# A hostile /ui: try native commands and report each outcome to the server log.
cat > "$WORK/hub/ui/index.html" <<'HTML'
<!doctype html><title>fake hub</title><script>
function report(k, v) { fetch('/report?' + new URLSearchParams({k: k, v: String(v).slice(0, 120)})); }
report('global', typeof window.__TAURI__);
var I = window.__TAURI_INTERNALS__;
if (I && I.invoke) {
  [['plugin:opener|open_url', {url: 'file:///etc/passwd'}], ['plugin:window|hide', {label: 'main'}],
   ['plugin:window-state|save_window_state', {flags: 1}], ['plugin:deep-link|get_current', {}],
   ['plugin:event|emit', {event: 'x', payload: 1}]].forEach(function (c) {
    I.invoke(c[0], c[1]).then(function () { report(c[0], 'ALLOWED'); }, function () { report(c[0], 'denied'); });
  });
}
</script>
HTML

fail() { echo "FAIL: $*"; exit 1; }
seen() { grep -q "$1" "$WORK/server.log"; }

# A cold `--toggle` (the first press of a Hyprland bind) must open the window.
if command -v xdotool >/dev/null; then
  "$BIN" --toggle --hub "http://127.0.0.1:$PORT" > "$WORK/cold.log" 2>&1 & COLD=$!
  sleep 4
  xdotool search --onlyvisible --name '^Unified Agent$' >/dev/null || { kill "$COLD"; fail "cold --toggle left the window hidden"; }
  kill "$COLD"; wait "$COLD" 2>/dev/null || true
  echo "ok   cold --toggle shows the window"
else
  echo "skip cold --toggle visibility (no xdotool)"
fi

"$BIN" --hub "http://127.0.0.1:$PORT" > "$WORK/app.log" 2>&1 & APP=$!
sleep 5
kill -0 "$APP" || fail "app exited while the hub was down"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/hub" > "$WORK/server.log" 2>&1 & SRV=$!
sleep 9
seen '"GET /healthz' || fail "offline page never probed the hub"
seen '"GET /ui' || fail "offline page did not move on to /ui"
echo "ok   offline page recovered to /ui"

timeout 10 "$BIN" --toggle || fail "--toggle second launch did not exit cleanly"
timeout 10 "$BIN" 'unified-agent://approvals?id=42' || fail "deep-link second launch did not exit cleanly"
sleep 3
seen 'GET /ui?view=approvals&id=42' || fail "deep link did not open /ui?view=approvals&id=42"
echo "ok   --toggle and deep link forwarded"
timeout 10 "$BIN" 'unified-agent://task/1556' || true
sleep 1
grep -q "not supported until the S2 router" "$WORK/app.log" || fail "object-path link was not refused"
echo "ok   object-path link refused"
kill -0 "$APP" || fail "app died during second launches"

grep -q 'k=global&v=undefined' "$WORK/server.log" || fail "window.__TAURI__ is exposed to the remote page"
denied=$(grep -c 'v=denied' "$WORK/server.log" || true)
! grep -q 'v=ALLOWED' "$WORK/server.log" || fail "remote page invoked a native command"
# The page loads more than once (startup, then the deep link), so at least 5.
[ "$denied" -ge 5 ] || fail "expected 5 denied invokes, saw $denied"
echo "ok   remote /ui: no __TAURI__ global, 5/5 native invokes denied"
echo "PASS"
