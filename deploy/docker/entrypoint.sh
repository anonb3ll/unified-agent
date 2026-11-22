#!/usr/bin/env bash
# Container entrypoint for the unified-agent hub image (task #1194).
#
# First start bootstraps $UA_HOME via `unified-agent init`. service.env is then
# sourced so operator edits apply, and the container-required settings (HTTP
# transport, wide bind, auth enforcement) are re-applied afterwards because the
# service.env template defaults to stdio/no-auth.
#
# Hosted platforms (task #2108). On Railway (RAILWAY_ENVIRONMENT_ID is set) or
# with UA_HOSTED=1:
#   - the platform's variables win over service.env, so a token or setting
#     injected through the dashboard is never shadowed by the volume copy.
#     Image ENV defaults are not platform variables: service.env and a real
#     runtime value win, and a default applies only when that key is unset;
#   - the bootstrap token is never printed: platform logs are retained and
#     readable by everyone on the project. It stays in service.env (0600) on
#     the volume, or inject UA_AGENT_TOKENS as a sealed variable instead;
#   - state must live on a mounted volume, or the start is refused, because an
#     ephemeral container filesystem loses the database on every redeploy;
#   - PORT, RAILWAY_PUBLIC_DOMAIN and RAILWAY_PRIVATE_DOMAIN feed the bind port,
#     the Host allowlist and UA_PUBLIC_URL.
#
# Off Railway (a VM, task #2148) the same image runs with UA_HOSTED=1, an
# explicit UA_PUBLIC_URL / UA_PUBLIC_HOSTS, and UA_STATE_MOUNT_PATH naming the
# mount that must hold UA_HOME (the host-neutral form of the volume guard).
set -euo pipefail

UA_HOME="${UA_HOME:-/data}"
UA_RUN_UID="${UA_RUN_UID:-10001}"

hosted=0
if [ -n "${RAILWAY_ENVIRONMENT_ID:-}" ] || [ "${UA_HOSTED:-0}" = "1" ]; then
    hosted=1
fi

# Railway mounts volumes owned by root, and an image that runs as a non-root
# uid cannot write to them; Railway's documented remedy is RAILWAY_RUN_UID=0.
# Started as root, take ownership of the state directory and drop to the
# image's unprivileged user before anything else runs. The hub never runs as
# root: if the drop fails, the start fails.
if [ "$(id -u)" = "0" ]; then
    if [ "${UA_ALLOW_ROOT:-0}" != "1" ]; then
        mkdir -p "$UA_HOME"
        # A fresh volume root is owned by root; after the first start it is ours,
        # so later starts skip the recursive walk over a large vault.
        if [ "$(stat -c %u "$UA_HOME")" != "$UA_RUN_UID" ]; then
            chown -R "$UA_RUN_UID:$UA_RUN_UID" "$UA_HOME"
        fi
        # python3 is the one privilege-drop tool the slim image is sure to have.
        exec python3 -I -c '
import os, pwd, sys
uid = int(sys.argv[1])
os.setgroups([])
os.setgid(uid)
os.setuid(uid)
if os.getuid() == 0 or os.geteuid() == 0:
    sys.exit("entrypoint: privilege drop failed")
try:
    os.environ["HOME"] = pwd.getpwuid(uid).pw_dir
except KeyError:
    pass
os.execv(sys.argv[2], sys.argv[2:])
' "$UA_RUN_UID" "$0" "$@"
    fi
    echo "[entrypoint] WARNING: UA_ALLOW_ROOT=1 — running the hub as root." >&2
fi

if [ "$hosted" = "1" ]; then
    # One hub, one org. Writers resolve it in edition.configured_org_id,
    # which reads WORKOS_ORGANIZATION_ID and then
    # UA_HUB_WORKOS_ORGANIZATION_ID. Copy the hosted name into the pilot
    # name before any process opens the store — including `unified-agent
    # init` below, which runs before service.env exists — so both processes
    # that share this data directory stamp that same org. Clients are
    # bearer tokens in this org, not extra orgs.
    platform_workos="${WORKOS_ORGANIZATION_ID:-}"
    platform_hub_org="${UA_HUB_WORKOS_ORGANIZATION_ID:-}"
    if [ -z "$platform_workos" ] && [ -n "$platform_hub_org" ]; then
        export WORKOS_ORGANIZATION_ID="$platform_hub_org"
    fi
    # Platform overrides only, captured before service.env can shadow them.
    # A value still equal to its image default is left out, so service.env can
    # replace it and the default remains only while the key is unset.
    startup_env="$(python3 -I - <<'PY'
import os
import re
import shlex

defaults = {
    "UA_HOME": "/data",
    "UA_WORKSPACE": "/data/workspace",
    "UA_TRANSPORT": "http",
    "UA_HTTP_HOST": "0.0.0.0",
    "UA_HTTP_PORT": "8765",
    "UA_ENFORCE_AUTH": "1",
    "UA_EDITION": "enterprise",
    "PYTHONUNBUFFERED": "1",
}
ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
for key, value in os.environ.items():
    if not ident.match(key) or defaults.get(key) == value:
        continue
    print(f"export {key}={shlex.quote(value)}")
PY
)"

    if [ -n "${RAILWAY_ENVIRONMENT_ID:-}" ] && [ -z "${RAILWAY_VOLUME_MOUNT_PATH:-}" ] \
        && [ "${UA_ALLOW_EPHEMERAL_STATE:-0}" != "1" ]; then
        echo "[entrypoint] refusing to start: no Railway volume is attached." >&2
        echo "[entrypoint] Attach a volume mounted at $UA_HOME; without one the database" >&2
        echo "[entrypoint] and tokens are lost on every redeploy." >&2
        echo "[entrypoint] (UA_ALLOW_EPHEMERAL_STATE=1 overrides this for a throwaway test.)" >&2
        exit 64
    fi
    if [ -n "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]; then
        mount="${RAILWAY_VOLUME_MOUNT_PATH%/}"
        case "${UA_HOME%/}/" in
            "$mount"/*) ;;
            *)
                echo "[entrypoint] refusing to start: UA_HOME=$UA_HOME is not on the" >&2
                echo "[entrypoint] Railway volume mounted at $RAILWAY_VOLUME_MOUNT_PATH." >&2
                echo "[entrypoint] Mount the volume at $UA_HOME, or set UA_HOME under it." >&2
                exit 64
                ;;
        esac
    fi
fi

# Host-neutral state guard (task #2148). Any host that is not Railway (a VM,
# compose, a bare docker run) names the mount that must hold the state with
# UA_STATE_MOUNT_PATH. The start is refused unless UA_HOME resolves onto that
# path and the deepest mount holding UA_HOME is that path, so a missing bind
# mount, an unmounted data disk, `..` or a symlink out of the mount, or another
# filesystem mounted inside it cannot silently put the database on ephemeral
# storage. Railway keeps its own guard above. Unset keeps the previous behaviour.
if [ -n "${UA_STATE_MOUNT_PATH:-}" ] && [ "${UA_ALLOW_EPHEMERAL_STATE:-0}" != "1" ]; then
    # Resolve '..' and symlinks first; a lexical prefix test is escapable.
    if ! state_mount="$(realpath -m -- "$UA_STATE_MOUNT_PATH")" \
        || ! state_home="$(realpath -m -- "$UA_HOME")"; then
        echo "[entrypoint] refusing to start: cannot resolve UA_HOME or UA_STATE_MOUNT_PATH." >&2
        exit 64
    fi
    # Prefixes are computed first: bash 5.3 mis-matches a quoted empty
    # expansion such as "${m%/}"/* when the mount is "/".
    home_prefix="${state_home%/}/"
    mount_prefix="${state_mount%/}/"
    case "$home_prefix" in
        "$mount_prefix"*) ;;
        *)
            echo "[entrypoint] refusing to start: UA_HOME=$UA_HOME (resolves to $state_home)" >&2
            echo "[entrypoint] is not under UA_STATE_MOUNT_PATH=$UA_STATE_MOUNT_PATH ($state_mount)." >&2
            exit 64
            ;;
    esac
    # The deepest mount point that contains the resolved state directory is
    # the filesystem the database will live on. Field 5 of mountinfo is the
    # mount point in this namespace (octal escapes such as \040 decoded).
    state_fs=""
    if [ -r /proc/self/mountinfo ]; then
        while read -r _ _ _ _ point _; do
            point="$(printf '%b' "$point")"
            point_prefix="${point%/}/"
            case "$home_prefix" in
                "$point_prefix"*)
                    if [ "${#point}" -gt "${#state_fs}" ]; then
                        state_fs="$point"
                    fi
                    ;;
            esac
        done </proc/self/mountinfo
    fi
    if [ "$state_fs" != "$state_mount" ]; then
        echo "[entrypoint] refusing to start: UA_STATE_MOUNT_PATH=$UA_STATE_MOUNT_PATH" >&2
        echo "[entrypoint] is not a mount point holding UA_HOME (its filesystem is" >&2
        echo "[entrypoint] mounted at '${state_fs:-unknown}'). Mount the data disk or volume" >&2
        echo "[entrypoint] there; without it the database and tokens are lost with the container." >&2
        echo "[entrypoint] (UA_ALLOW_EPHEMERAL_STATE=1 overrides this for a throwaway test.)" >&2
        exit 64
    fi
fi

if [ ! -f "$UA_HOME/service.env" ]; then
    if [ "$hosted" = "1" ]; then
        init_json="$(unified-agent init --json)"
        label="$(printf '%s' "$init_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token_label") or "")')"
        if [ -n "${UA_AGENT_TOKENS:-}" ]; then
            echo "[entrypoint] initialised $UA_HOME; using the injected UA_AGENT_TOKENS."
        else
            cat <<EOF
================ unified-agent bootstrap ================
Initialised $UA_HOME. Bearer tokens for '${label}' and the operator were
written to $UA_HOME/service.env (mode 0600). They are not printed here because
platform logs are shared and retained. Read them over the platform shell
(railway ssh), or set UA_AGENT_TOKENS as a sealed variable and redeploy.
==========================================================
EOF
        fi
    else
        init_json="$(unified-agent init --json --print-token)"
        token="$(printf '%s' "$init_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token") or "")')"
        label="$(printf '%s' "$init_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token_label") or "")')"
        cat <<EOF
================ unified-agent bootstrap token ================
label: ${label}
token: ${token}
This is the only time this token is shown. Rotate it via UA_AGENT_TOKENS in $UA_HOME/service.env.
===============================================================
EOF
    fi
fi

set -a
# shellcheck source=/dev/null
. "$UA_HOME/service.env"
set +a

if [ "$hosted" = "1" ]; then
    # Platform overrides outrank the volume's service.env.
    eval "$startup_env"
    # One flag for the process, after the platform restore so a snapshot
    # that left UA_HOSTED unset cannot drop it. Railway sets
    # RAILWAY_ENVIRONMENT_ID and may leave UA_HOSTED unset; the hub treats
    # either as hosted (task #2109).
    export UA_HOSTED=1
    # service.env must not blank or replace the platform org. An explicit
    # platform WORKOS_ORGANIZATION_ID was restored by the eval above. When
    # the platform only set UA_HUB_WORKOS_ORGANIZATION_ID, put that back.
    # When neither was on the platform, an org that arrived via service.env
    # still becomes the tenant every later process stamps.
    if [ -z "$platform_workos" ] && [ -n "$platform_hub_org" ]; then
        export WORKOS_ORGANIZATION_ID="$platform_hub_org"
    elif [ -z "${WORKOS_ORGANIZATION_ID:-}" ] && [ -n "${UA_HUB_WORKOS_ORGANIZATION_ID:-}" ]; then
        export WORKOS_ORGANIZATION_ID="$UA_HUB_WORKOS_ORGANIZATION_ID"
    fi
    if [ -n "${PORT:-}" ]; then
        export UA_HTTP_PORT="$PORT"
    fi
    if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ] && [ -z "${UA_PUBLIC_URL:-}" ]; then
        export UA_PUBLIC_URL="https://${RAILWAY_PUBLIC_DOMAIN}"
    fi
    hosted_hosts=""
    for name in "${RAILWAY_PUBLIC_DOMAIN:-}" "${RAILWAY_PRIVATE_DOMAIN:-}"; do
        if [ -n "$name" ]; then
            hosted_hosts="${hosted_hosts:+$hosted_hosts,}${name},${name}:*"
        fi
    done
    # Railway's healthcheck probe sends this Host header; without it every
    # deploy fails its healthcheck against the DNS-rebinding allowlist.
    if [ -n "${RAILWAY_ENVIRONMENT_ID:-}" ]; then
        hosted_hosts="${hosted_hosts:+$hosted_hosts,}healthcheck.railway.app"
    fi
    if [ -n "$hosted_hosts" ]; then
        UA_PUBLIC_HOSTS="${UA_PUBLIC_HOSTS:+$UA_PUBLIC_HOSTS,}$hosted_hosts"
    fi
fi

# Container-required values — applied after the source so a stale or
# template-default service.env cannot demote the container to stdio/loopback.
export UA_TRANSPORT=http
export UA_HTTP_HOST=0.0.0.0
if [ "${UA_DOCKER_ALLOW_INSECURE:-0}" = "1" ] && [ "$hosted" != "1" ]; then
    echo "[entrypoint] WARNING: UA_DOCKER_ALLOW_INSECURE=1 — UA_ENFORCE_AUTH was NOT" >&2
    echo "[entrypoint] re-applied; the hub may be serving without auth enforcement." >&2
    echo "[entrypoint] This is a debugging escape hatch, not a supported posture." >&2
else
    export UA_ENFORCE_AUTH=1
fi

# Host-header allowlist (DNS-rebinding protection). Only default it when the
# operator did not set one: 'name:*' is the any-port form accepted by both the
# MCP transport-security middleware and webui/auth.py. Loopback + localhost
# covers the in-container healthcheck and `-p 8765:8765` publishes; extra
# hostnames go in UA_PUBLIC_HOSTS (comma-separated). Entries in the existing
# UA_HTTP_PUBLIC_HOSTS (colon-separated, public-internet set) must also be in
# the allowlist, so they are folded in with ':'->',' normalization.
if [ -z "${UA_HTTP_ALLOWED_HOSTS:-}" ]; then
    allowed="127.0.0.1:*,localhost:*"
    if [ -n "${UA_PUBLIC_HOSTS:-}" ]; then
        allowed="${allowed},${UA_PUBLIC_HOSTS}"
    fi
    if [ -n "${UA_HTTP_PUBLIC_HOSTS:-}" ]; then
        allowed="${allowed},$(printf '%s' "$UA_HTTP_PUBLIC_HOSTS" | tr ':' ',')"
    fi
    export UA_HTTP_ALLOWED_HOSTS="$allowed"
fi

# Passthrough for `docker exec <name> /app/deploy/docker/entrypoint.sh <cmd>`:
# runs the command with the full hub environment (service.env + overrides).
# With no arguments, start the hub.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Scheduled backups on a hosted hub (task #2585). There is no systemd timer or
# launchd agent in a container, and a separate cron service cannot mount this
# volume, so the schedule runs beside the hub, as the same unprivileged user,
# with the same environment. It is opt-in: UA_BACKUP_INTERVAL_S unset starts
# nothing. With UA_BACKUP_S3_URI set, each snapshot is also copied offsite.
if [ "$hosted" = "1" ] && [ -n "${UA_BACKUP_INTERVAL_S:-}" ]; then
    unified-agent-backup-loop &
    # The loop raises the interval to at least an hour and logs the value it
    # uses ("started", interval_s), so this line does not repeat the raw one.
    echo "[entrypoint] started the backup schedule (pid $!); see its \"started\" line for the interval."
fi
exec unified-agent
