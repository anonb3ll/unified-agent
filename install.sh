#!/usr/bin/env bash
# install.sh — cold-start installer for unified-agent on a foreign machine.
#
# Wraps the documented cold-start flow (docs/installation.md): uv tool install
# -> unified-agent init -> unified-agent doctor. It does not invent a second
# install path — every step here is the same one a human is told to run.
#
# Remote use (public or credentialed remote):
#   curl -fsSL <install.sh URL> -o install.sh
#   echo "<expected sha256>  install.sh" | sha256sum -c -   # verify before running
#   bash install.sh --repo https://github.com/example/project --ref v0.3.2
#
# Local use (private repo, no credentials on the machine):
#   bash install.sh --local /path/to/checkout
#
# Guarantees:
#   - refuses to run as root (a hub owned by root is a misconfiguration)
#   - idempotent: re-running converges to the same state (uv --force reinstall,
#     init never rewrites an existing service.env)
#   - installs a pinned uv (checksum-verified) only when uv is missing
#   - exits non-zero if doctor reports a MISCONFIGURED capability or degraded
#     health for an explicitly registered integration
#
# Env:
#   UA_REPO_URL       same as --repo (required for remote installs)
#   UA_REF            same as --ref (flag wins)
#   UA_LOCAL_SOURCE   same as --local (flag wins)
#   UA_UV_VERSION     uv version to install when uv is missing (default pinned)
#   UA_SKIP_DOCTOR=1  skip doctor only; transport/auth safety gates still run

set -euo pipefail

REPO_URL="${UA_REPO_URL:-}"
DEFAULT_REF="v0.3.2"
UV_VERSION="${UA_UV_VERSION:-0.11.32}"
# sha256 of https://astral.sh/uv/0.11.32/install.sh — bump with UA_UV_VERSION.
UV_INSTALLER_SHA256="43aff33a967fe40e8c17949d8c85c65bc43f3b5c94742393c957f56ab5ba80f4"

REF="${UA_REF:-$DEFAULT_REF}"
LOCAL_SOURCE="${UA_LOCAL_SOURCE:-}"

usage() {
    sed -n '2,28p' "$0"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)  REPO_URL="${2:?--repo needs a repository URL}"; shift 2 ;;
        --ref)   REF="${2:?--ref needs a tag, branch, or sha}"; shift 2 ;;
        --local) LOCAL_SOURCE="${2:?--local needs a path}"; shift 2 ;;
        --help|-h) usage 0 ;;
        *) echo "install.sh: unknown argument $1" >&2; usage 2 ;;
    esac
done

if [[ "$(id -u)" -eq 0 ]]; then
    echo "install.sh: refusing to run as root — install as the user who will own the hub." >&2
    exit 1
fi

log() { printf 'install.sh: %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# Validate the source before downloading tools or installing anything.
if [[ -n "$LOCAL_SOURCE" ]]; then
    [[ -f "$LOCAL_SOURCE/pyproject.toml" ]] || die "--local path has no pyproject.toml"
else
    [[ -n "$REPO_URL" ]] || die "set UA_REPO_URL or pass --repo, or use --local"
    [[ "$REPO_URL" =~ ^https://[^/@[:space:]?#]+(/[^[:space:]?#]*)?$ && "$REPO_URL" != *'@'* ]] \
        || die "--repo must be an HTTPS repository URL without embedded credentials, query or fragment"
fi

# --- uv ----------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    log "uv already installed ($(uv --version)) — skipping uv install"
else
    log "installing uv ${UV_VERSION} (checksum-verified)"
    tmp_installer="$(mktemp)"
    trap 'rm -f "$tmp_installer"' EXIT
    curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$tmp_installer" \
        || die "could not fetch uv installer for ${UV_VERSION}"
    if [[ "${UV_VERSION}" == "0.11.32" ]]; then
        actual="$(sha256sum "$tmp_installer" | awk '{print $1}')"
        [[ "$actual" == "$UV_INSTALLER_SHA256" ]] \
            || die "uv installer checksum mismatch: got ${actual}, expected ${UV_INSTALLER_SHA256}"
        log "uv installer checksum OK"
    else
        log "WARNING: non-default uv version — installer NOT checksum-verified"
    fi
    sh "$tmp_installer" >/dev/null
    rm -f "$tmp_installer"; trap - EXIT
fi

export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || die "uv not on PATH after install (expected ~/.local/bin/uv)"

# --- unified-agent -------------------------------------------------------------
if [[ -n "$LOCAL_SOURCE" ]]; then
    [[ -f "$LOCAL_SOURCE/pyproject.toml" ]] \
        || die "--local path has no pyproject.toml: $LOCAL_SOURCE"
    SRC="${LOCAL_SOURCE%/}[pdf]"
    log "installing unified-agent[pdf] from local source: $LOCAL_SOURCE"
else
    SRC="unified-agent[pdf] @ git+${REPO_URL}@${REF}"
    log "installing unified-agent[pdf] from git ref: $REF"
fi

# --force makes re-runs converge to the requested ref instead of failing on
# "already installed" — the idempotent outcome is the same either way.
uv tool install --force "$SRC" || die "uv tool install failed"

command -v unified-agent >/dev/null 2>&1 \
    || die "unified-agent not on PATH after install (expected ~/.local/bin/unified-agent)"

# --- init + doctor -------------------------------------------------------------
# init is idempotent: it never rewrites an existing service.env (no token
# rotation) and never touches an existing database.
log "running unified-agent init"
unified-agent init

# A cold install has no hub service unit yet. Doctor must inspect the same
# initialized configuration that the hub will use, rather than an empty
# installer process environment that it correctly reports as unknowable.
ENV_FILE="${UA_HOME:-$HOME/.unified_agent}/service.env"
[[ -r "$ENV_FILE" ]] || die "init did not create a readable service.env"
set -a
. "$ENV_FILE"
set +a
if [[ "${UA_TRANSPORT:-stdio}" != "http" && "${UA_ENFORCE_AUTH:-0}" == "1" ]]; then
    # Stdio has no HTTP-authenticated identity, so enforcement here would
    # imply a protection that the transport cannot provide.
    die "UA_ENFORCE_AUTH=1 requires UA_TRANSPORT=http; stdio has no authenticated identity"
fi
if [[ "${UA_TRANSPORT:-stdio}" == "http" ]]; then
    # HTTP can authenticate with a static bearer token, MCP OAuth, or the
    # enterprise WorkOS hub leg. A partial OAuth configuration is rejected by
    # doctor below; the hub itself fails closed if a configured leg cannot build.
    if [[ -z "${UA_AGENT_TOKENS:-}" && "${UA_MCP_OAUTH:-0}" != "1" \
        && -z "${UA_HUB_WORKOS_ORGANIZATION_ID:-}" ]]; then
        die "authenticated HTTP transport requires a configured auth provider"
    fi
fi
if [[ "${UA_TRANSPORT:-stdio}" == "http" ]]; then
    if [[ -z "${UA_WEB_DIST_DIR:-}" ]]; then
        log "HTTP /ui serves the Control Center packaged at install time; if doctor reports web_ui MISCONFIGURED, the source had no UI build and /ui answers 503 (the install still completes): build packages/protocol, client and web, then re-run this installer (UA_WEB_DIST_DIR is only for a custom or development build)"
    elif [[ ! -f "${UA_WEB_DIST_DIR}/index.html" ]]; then
        log "warning: UA_WEB_DIST_DIR has no index.html; it overrides the packaged Control Center, so /ui will return 503 until that build exists or the variable is removed"
    else
        log "UA_WEB_DIST_DIR overrides the packaged Control Center; remove it from service.env to serve the installed build"
    fi
fi
export UA_MCP_OAUTH="${UA_MCP_OAUTH:-0}"
export UA_APPROVAL_TOOLS="${UA_APPROVAL_TOOLS:-}"

if [[ "${UA_SKIP_DOCTOR:-0}" == "1" ]]; then
    log "UA_SKIP_DOCTOR=1 — skipping doctor gate"
else
    log "running unified-agent doctor (fails on MISCONFIGURED or degraded registered integrations)"
    if [[ "${UA_TRANSPORT:-stdio}" == "http" && -z "${UA_WEB_DIST_DIR:-}" ]]; then
        # An ordinary build of an unbuilt tree installs without the UI. Doctor
        # still reports web_ui; only that expected state is tolerated here.
        unified-agent doctor --allow-unbuilt-ui
    else
        unified-agent doctor
    fi
fi

cat <<'NEXT'

install.sh: done. Next steps:

  set -a; . "${UA_HOME:-$HOME/.unified_agent}/service.env"; set +a   # load the env
  UA_TRANSPORT=http UA_ENFORCE_AUTH=1 unified-agent                  # serve over HTTP

  The generated bearer token lives in service.env (UA_AGENT_TOKENS) — it was
  NOT printed here. Rotate it if this install ever leaves your control.
  See docs/installation.md for hub mode, systemd unit, and backup timer.
NEXT
