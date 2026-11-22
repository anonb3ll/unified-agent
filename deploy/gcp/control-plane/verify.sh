#!/usr/bin/env bash
# Acceptance checks for the control-plane VM (task #2148). Runs as root ON the
# VM (bootstrap.sh calls it over IAP SSH; installed at
# /usr/local/lib/unified-agent/verify.sh). Read-only unless --restart.
#
# Usage:
#   verify.sh [--restart]
#
# Checks:
#   - ua-hub.service active; the data disk mounted and bound at /data
#   - /healthz 200, /readyz 200 with "ok": true, unauthenticated POST /mcp 401
#   - the hub publishes on loopback only; the VM has no external IP
#   - Tailscale Serve carries :8765 and :443, Funnel is off, and the hub
#     answers /healthz through the MagicDNS name
#   - system_doctor in the container: execution_locality LIVE, nothing
#     MISCONFIGURED
#   - execution-locality refusal: a harness launch from a hub-environment
#     process is refused by the devices-only guard
#   - --restart: restart-retains-state (a nonce on the disk, service.env, and
#     the tasks/memory row counts survive a service restart). This restarts
#     the hub; use it at stand-up or in a maintenance window.
#
# Prints PASS/FAIL per check and no secret values. Exit 0 only if all pass.
# pass() always returns 0, so `cond && pass .. || fail ..` is a safe if/else.
# shellcheck disable=SC2015
set -uo pipefail

RESTART=0
case "${1:-}" in
  --restart) RESTART=1 ;;
  "") ;;
  -h|--help) sed -n '2,24p' "$0" | sed 's/^# \?//'; exit 0 ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

DATA_MOUNT=/mnt/ua-data
PORT=8765
BASE="http://127.0.0.1:${PORT}"
CONTAINER=ua-hub
failures=0

pass() { printf '[PASS] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; failures=$((failures + 1)); }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
in_hub() { docker exec "$CONTAINER" /app/deploy/docker/entrypoint.sh "$@"; }

wait_ready() {
  for _ in $(seq 1 90); do
    [[ "$(http_code "$BASE/readyz")" == "200" ]] && return 0
    sleep 2
  done
  return 1
}

# Read-only counts straight from the store, inside the container as the hub
# user. Prints "<tasks> <memories>".
state_counts() {
  in_hub python3 -I -c '
import sqlite3
c = sqlite3.connect("file:/data/memory.db?mode=ro", uri=True)
def n(t):
    try:
        return c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    except sqlite3.Error:
        return -1
print(n("tasks"), n("memory"))
' 2>/dev/null
}

# --- service and disk --------------------------------------------------------
check "ua-hub.service is active" systemctl is-active --quiet ua-hub.service
check "data disk mounted at $DATA_MOUNT" mountpoint -q "$DATA_MOUNT"
bind_src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$bind_src" == "$DATA_MOUNT" ]]; then
  pass "container /data is the data disk ($bind_src)"
else
  fail "container /data source is '${bind_src:-none}', expected $DATA_MOUNT"
fi

# backup.sh uploads through sync-backup-to-gcs.sh, which needs gcloud or gsutil.
if command -v gcloud >/dev/null 2>&1 || command -v gsutil >/dev/null 2>&1; then
  pass "GCS client present for the offsite backup"
else
  fail "no gcloud or gsutil on PATH; the offsite backup cannot upload"
fi

# --- probes ------------------------------------------------------------------
code="$(http_code "$BASE/healthz")"
[[ "$code" == "200" ]] && pass "/healthz 200" || fail "/healthz returned $code"
ready_body="$(curl -s --max-time 10 "$BASE/readyz" || true)"
code="$(http_code "$BASE/readyz")"
if [[ "$code" == "200" ]] && printf '%s' "$ready_body" | grep -q '"ok": *true'; then
  pass "/readyz 200 and ok"
else
  fail "/readyz returned $code"
fi
code="$(http_code -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/mcp")"
[[ "$code" == "401" ]] && pass "unauthenticated POST /mcp is 401" || fail "unauthenticated POST /mcp returned $code"

# --- no public ingress -------------------------------------------------------
wide="$(ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' \
  | grep -vE '^100\.|^\[fd7a:' || true)"
[[ -z "$wide" ]] && pass "port $PORT listens on loopback/tailnet only" || fail "port $PORT also listens on: $wide"
ext_ip="$(curl -s --max-time 5 -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip' || true)"
[[ -z "$ext_ip" || "$ext_ip" == *"Not Found"* || "$ext_ip" == *"<"* ]] \
  && pass "VM has no external IP" || fail "VM has an external IP"

# --- Tailscale Serve ---------------------------------------------------------
serve_json="$(tailscale serve status --json 2>/dev/null || true)"
check "Tailscale Serve forwards :$PORT" grep -q "\"$PORT\"" <<<"$serve_json"
check "Tailscale Serve terminates :443" grep -q '"443"' <<<"$serve_json"
funnel="$(python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
except ValueError:
    print("UNREADABLE"); sys.exit(0)
print("ON" if any((cfg.get("AllowFunnel") or {}).values()) else "OFF")
' <<<"${serve_json:-null}" 2>/dev/null || echo UNREADABLE)"
[[ "$funnel" == "OFF" ]] && pass "Tailscale Funnel is off" || fail "Tailscale Funnel is $funnel (must be off)"
ts_dns="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
if [[ -n "$ts_dns" ]]; then
  code="$(http_code "https://${ts_dns}/healthz")"
  [[ "$code" == "200" ]] && pass "/healthz 200 via https://<magicdns>" || fail "/healthz via Serve HTTPS returned $code"
  code="$(http_code "http://${ts_dns}:${PORT}/healthz")"
  [[ "$code" == "200" ]] && pass "/healthz 200 via <magicdns>:$PORT" || fail "/healthz via Serve TCP returned $code"
else
  fail "could not read this node's MagicDNS name"
fi

# --- system_doctor -----------------------------------------------------------
doctor_json="$(in_hub unified-agent doctor --json 2>/dev/null || true)"
doctor_summary="$(printf '%s' "$doctor_json" | python3 -c '
import json, sys
try:
    report = json.load(sys.stdin)
except ValueError:
    print("UNREADABLE"); sys.exit(0)
checks = report.get("checks", [])
by = {c.get("capability"): c.get("status") for c in checks}
bad = sorted(c.get("capability") for c in checks if c.get("status") == "MISCONFIGURED")
counts = {}
for c in checks:
    counts[c.get("status")] = counts.get(c.get("status"), 0) + 1
print(by.get("execution_locality", "ABSENT"))
print(",".join(bad))
print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))
' 2>/dev/null || echo UNREADABLE)"
locality_status="$(sed -n 1p <<<"$doctor_summary")"
misconfigured="$(sed -n 2p <<<"$doctor_summary")"
counts="$(sed -n 3p <<<"$doctor_summary")"
[[ "$locality_status" == "LIVE" ]] && pass "doctor execution_locality LIVE" \
  || fail "doctor execution_locality is ${locality_status:-UNREADABLE}"
[[ -z "$misconfigured" && "$locality_status" != "UNREADABLE" ]] && pass "doctor: nothing MISCONFIGURED ($counts)" \
  || fail "doctor MISCONFIGURED: ${misconfigured:-report unreadable}"

# --- execution-locality refusal ----------------------------------------------
refusal="$(in_hub python3 -I -c '
import subprocess
from unified_agent import execution_locality as el
if not el.devices_only():
    print("NOT_DEVICES_ONLY"); raise SystemExit
el.install_process_guard()
try:
    subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
except el.ServerExecutionRefused:
    print("REFUSED")
except Exception as exc:
    print("NOT_REFUSED:" + type(exc).__name__)
else:
    print("NOT_REFUSED:ran")
' 2>/dev/null || true)"
[[ "$refusal" == "REFUSED" ]] && pass "harness launch refused (UA_EXECUTION_LOCALITY=devices)" \
  || fail "harness launch was not refused (${refusal:-no output})"

# --- restart retains state ---------------------------------------------------
if (( RESTART == 1 )); then
  marker_dir="$DATA_MOUNT/.ua-verify"
  install -d -m 0700 "$marker_dir"
  nonce="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '%s\n' "$nonce" >"$marker_dir/restart-marker"
  env_sum_before="$(sha256sum "$DATA_MOUNT/service.env" 2>/dev/null | cut -d' ' -f1)"
  counts_before="$(state_counts)"
  started_before="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || true)"
  systemctl restart ua-hub.service
  if wait_ready; then
    pass "hub ready again after restart"
  else
    fail "hub not ready within 180 s of restart"
  fi
  started_after="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || true)"
  [[ -n "$started_after" && "$started_after" != "$started_before" ]] \
    && pass "container was really restarted" || fail "container start time unchanged"
  [[ "$(cat "$marker_dir/restart-marker" 2>/dev/null)" == "$nonce" ]] \
    && pass "disk marker survived restart" || fail "disk marker lost"
  rm -f "$marker_dir/restart-marker"
  env_sum_after="$(sha256sum "$DATA_MOUNT/service.env" 2>/dev/null | cut -d' ' -f1)"
  [[ -n "$env_sum_before" && "$env_sum_before" == "$env_sum_after" ]] \
    && pass "service.env (tokens) unchanged across restart" || fail "service.env changed or missing"
  counts_after="$(state_counts)"
  read -r tasks_b mem_b <<<"${counts_before:--1 -1}"
  read -r tasks_a mem_a <<<"${counts_after:--1 -1}"
  if (( tasks_b >= 0 && mem_b >= 0 && tasks_a >= tasks_b && mem_a >= mem_b )); then
    pass "store kept its rows (tasks $tasks_b -> $tasks_a, memories $mem_b -> $mem_a)"
  else
    fail "store rows before/after restart: tasks $tasks_b -> $tasks_a, memories $mem_b -> $mem_a"
  fi
fi

echo
if (( failures == 0 )); then
  echo "CONTROL PLANE VERIFIED"
  exit 0
fi
echo "CONTROL PLANE NOT VERIFIED: $failures check(s) failed"
exit 1
