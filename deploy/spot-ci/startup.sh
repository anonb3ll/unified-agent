#!/usr/bin/env bash
# Startup script of the Spot CI VM `ua-ci-runner` (task #2252).
#
# GCE runs this as root on every boot. It drains the run queue in
# gs://<bucket>/queue/ in submission order, running each with the
# executor.py uploaded beside that run, then powers the VM off. A stopped VM
# bills only its disk; scripts/cloud-ci.sh starts it again when it queues a
# run. The instance's max-run-duration stops it even if this script hangs.
#
# A run found WORKING at boot was cut short (Spot preemption or the duration
# cap): it is retried once, then reported as INTERNAL_ERROR.
set -uo pipefail

md() {
    curl -fsS -H 'Metadata-Flavor: Google' \
        "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}
bucket="$(md ua-ci-bucket)"
log() { echo "ua-ci-runner: $*" | systemd-cat -t ua-ci-runner; echo "ua-ci-runner: $*"; }

if ! command -v docker >/dev/null 2>&1 || ! python3 -c 'import yaml' 2>/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io python3-yaml
fi
# Docker Hub images through Google's pull-through mirror, as on Cloud Build.
if [ ! -f /etc/docker/daemon.json ]; then
    mkdir -p /etc/docker
    printf '{"registry-mirrors": ["https://mirror.gcr.io"]}\n' > /etc/docker/daemon.json
    systemctl restart docker
fi
systemctl start docker

run_status() { # prefix; prints the run's status, or nothing when unreadable
    gcloud storage cat "$1/state.json" 2>/dev/null \
        | python3 -I -c 'import json, sys; print(json.load(sys.stdin).get("status", ""))' 2>/dev/null
}
settled() { # status
    case "$1" in SUCCESS|FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED) return 0 ;; esac
    return 1
}
# GCE's 3 h limit spans boot, execution and cleanup. Admit a full executor
# budget only while it fits, and enforce that budget even for older executors
# or a ci.yaml whose timeout is longer than the VM can support (#2613/#2646).
MAX_RUN_S="${UA_CI_MAX_RUN_S:-10800}"
RUN_BUDGET_S="${UA_CI_RUN_BUDGET_S:-9600}"
CLEANUP_S=300
UPTIME_FILE="${UA_CI_UPTIME_FILE:-/proc/uptime}"
run_fits() { # true when a run started now ends before the duration cap
    local up
    up="$(cut -d. -f1 "$UPTIME_FILE" 2>/dev/null)"
    [[ "$up" =~ ^[0-9]+$ ]] || return 1
    (( up + RUN_BUDGET_S + CLEANUP_S < MAX_RUN_S ))
}
run_executor() {
    # Soft deadline in the current executor, hard limit for older revisions
    # and for a hung subprocess. Leave time to publish before the hard limit.
    UA_SPOT_BUILD_TIMEOUT_S="$((RUN_BUDGET_S - 120))" \
        timeout --kill-after=15s "${RUN_BUDGET_S}s" python3 -I /opt/ua-ci-executor.py "$@"
}
# The queue marker is the only record that a run still needs work: remove it
# only once the run has a final state, and retry a failed removal.
dequeue() { # marker
    local _
    for _ in 1 2 3; do
        gcloud storage rm -q "$1" && return 0
        sleep 5
    done
    log "could not remove $1; the next scan skips it as settled"
}
# Settle a run this script cannot execute, so its watcher stops waiting. A
# missing or corrupt state.json is rebuilt from meta.json, or from nothing, in the shape spot_ci.py submit writes: a status the watcher and
# --reconcile can read, never a skipped write that leaves the run unsettled.
fail_run() { # prefix reason [status]
    local status="${3:-INTERNAL_ERROR}"
    log "${1##*/}: $2; marking $status"
    local tmp
    tmp="$(mktemp -d)"
    # Rebuild only a state.json that is absent (the run's listing reads and
    # does not name it) or corrupt. A failed read of one that exists may be
    # transient and may hide a final result, so it is left for the next scan.
    if gcloud storage cat "$1/state.json" > "$tmp/state.in" 2>/dev/null; then
        :
    elif listing="$(gcloud storage ls "$1/" 2>/dev/null)" \
        && ! grep -q '/state\.json$' <<< "$listing"; then
        : > "$tmp/state.in"
    else
        log "${1##*/}: state.json exists but could not be read; leaving it for the next scan"
        rm -rf "$tmp"
        return 1
    fi
    gcloud storage cat "$1/meta.json" > "$tmp/meta.in" 2>/dev/null || : > "$tmp/meta.in"
    python3 -I -c '
import datetime, json, sys
run_id, state_path, meta_path, final_status = sys.argv[1:5]
def load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
state = load(state_path)
if state is None:
    state = {k: v for k, v in (load(meta_path) or {}).items() if k != "config"}
    state.update(id=run_id, steps=[])
    state.setdefault("createTime", datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
elif state.get("status") in {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED"}:
    sys.exit(1)  # an earlier unreadable status was transient; keep the result
state["status"] = final_status
state["finishTime"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
print(json.dumps(state))
' "${1##*/}" "$tmp/state.in" "$tmp/meta.in" "$status" > "$tmp/state.json" \
        && gcloud storage cp -q "$tmp/state.json" "$1/state.json"
    local rc=$?
    rm -rf "$tmp"
    return $rc
}

idle=0
while :; do
    next="$(gcloud storage ls "gs://$bucket/queue/" 2>/dev/null | sort | head -n 1)"
    if [ -z "$next" ]; then
        # A run queued while the VM was going idle would otherwise wait for
        # the submitter's next start call; give it a short window.
        (( idle++ >= 3 )) && break
        sleep 20
        continue
    fi
    idle=0
    run_id="${next##*_}"
    prefix="gs://$bucket/runs/$run_id"
    # A settled run whose marker survived (a failed removal) never reruns.
    if settled "$(run_status "$prefix")"; then
        dequeue "$next"
        continue
    fi
    if ! run_fits; then
        log "too close to the duration cap to start $run_id; powering off for a fresh window"
        break
    fi
    attempts="$(gcloud storage cat "$prefix/attempts" 2>/dev/null || echo 0)"
    [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=0
    if (( attempts >= 2 )); then
        fail_run "$prefix" "interrupted twice"
        dequeue "$next"
        continue
    fi
    if ! gcloud storage cp -q "$prefix/executor.py" /opt/ua-ci-executor.py; then
        fail_run "$prefix" "executor.py unreadable"
    elif ! run_fits; then
        log "executor staging used the remaining start window for $run_id; powering off"
        break
    else
        echo $((attempts + 1)) > /tmp/attempts
        if ! timeout 30s gcloud storage cp -q /tmp/attempts "$prefix/attempts"; then
            log "could not record attempt for $run_id; powering off"
            break
        fi
        log "running $run_id (attempt $((attempts + 1)))"
        if run_executor "$bucket" "$run_id"; then
            :
        else
            rc=$?
            if [[ "$rc" == 124 || "$rc" == 137 ]]; then
                fail_run "$prefix" "Spot runtime budget exceeded" TIMEOUT
                settled "$(run_status "$prefix")" && dequeue "$next"
                # A killed legacy executor may leave a running container.
                # Power off before attempting another build on this boot.
                break
            fi
            log "executor exited non-zero for $run_id"
        fi
    fi
    # An executor that died before publishing its result must not leave the
    # watcher waiting on a run nobody will execute.
    settled "$(run_status "$prefix")" || fail_run "$prefix" "executor ended without a result"
    dequeue "$next"
    docker container prune -f >/dev/null 2>&1
    docker image prune -f >/dev/null 2>&1
done
log "powering off"
shutdown -h now
