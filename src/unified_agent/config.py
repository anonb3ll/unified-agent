"""Paths and environment-variable settings (all overridable via UA_* env vars)."""

import contextvars
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import socket
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

from . import projects as _projects
from . import settings as _settings

logger = logging.getLogger(__name__)

# Seed os.environ from the user's settings.toml before any default below is
# read. Real environment variables always win; see settings.py.
_settings.apply_to_environ()

# Fallback sandbox root when neither UA_WORKSPACE nor settings.toml names one:
# a directory inside UA_HOME, so a fresh install never reaches outside it.
DEFAULT_WORKSPACE = Path("~/.unified_agent/workspace")


def default_workspace() -> Path:
    """``$UA_HOME/workspace`` — the workspace used when none is configured."""
    return data_home() / "workspace"


# Vault subdirectories agents may write to (colon-separated, vault-relative):
# Inbox = raw captures, Logs = journal, Wiki = agent-maintained wiki layer.
DEFAULT_VAULT_WRITE_DIRS = "00 - Inbox:Logs:Wiki"

DEFAULT_SHELL_ALLOWLIST = (
    "ls cat head tail grep find wc sed awk git python3 uv pytest ruff "
    "diff sort uniq which echo date"
)

# Hard limits (not env-configurable; documented in tool docstrings)
FETCH_TIMEOUT_S = 20
FETCH_MAX_BYTES = 2 * 1024 * 1024
WRITE_MAX_BYTES = 2 * 1024 * 1024
# Exports written straight to a vault file (write_to_vault=True) never travel
# back through an MCP response, so they get a larger ceiling than WRITE_MAX_BYTES
# — a full memory/task backup outgrows 2 MB. The inline path still uses
# WRITE_MAX_BYTES.
EXPORT_VAULT_MAX_BYTES = 32 * 1024 * 1024
READ_MAX_BYTES = 1 * 1024 * 1024
CMD_TIMEOUT_CAP_S = 120
CMD_OUTPUT_CAP = 50 * 1024
SEARCH_FILE_MAX_BYTES = 1 * 1024 * 1024


class WorkspaceDenied:
    """Sentinel for an identity explicitly denied hub-mediated filesystem access.

    Distinct from "no UA_AGENT_WORKSPACES entry" (falls through to the shared
    UA_WORKSPACE root) — this marks an identity that must NOT get any workspace,
    shared or otherwise. See agent_workspaces()'s "DENY" value.
    """

    def __repr__(self) -> str:
        return "<WorkspaceDenied>"


WORKSPACE_DENIED = WorkspaceDenied()


workspace_root_var: contextvars.ContextVar[Path | WorkspaceDenied | None] = contextvars.ContextVar(
    "ua_workspace_root", default=None
)

episode_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_episode_id", default=None
)

causal_parent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_causal_parent_id", default=None
)


def current_episode_id() -> str | None:
    """The active episode_id bound to the current context, if set."""
    return episode_id_var.get()


def current_causal_parent_id() -> str | None:
    """The active causal_parent_id bound to the current context, if set."""
    return causal_parent_id_var.get()


def policy_file_path() -> Path:
    """Path to the policy.yaml DSL configuration file."""
    return Path(os.environ.get("UA_POLICY_FILE", "~/.config/agent_hub/policy.yaml")).expanduser()


def project_delivery_file_path() -> Path:
    """Path to the per-project delivery-mode YAML (task #1026)."""
    return Path(
        os.environ.get(
            "UA_PROJECT_DELIVERY_FILE",
            str(data_home() / "project_delivery.yaml"),
        )
    ).expanduser()


def workspace_root() -> Path:
    """The sandbox root for fs_*/run_cmd tools: ContextVar override, else UA_WORKSPACE.

    The ContextVar lets the HTTP hub's auth middleware bind a per-agent workspace
    (see ``agent_workspaces()``) for the duration of one request — the same
    request-scoped-override pattern as ``current_source_host()`` — so a single
    shared hub registration can safely serve multiple projects without each
    needing its own spawned stdio process pinned to a fixed UA_WORKSPACE.
    """
    override = workspace_root_var.get()
    if isinstance(override, WorkspaceDenied):
        from mcp.server.mcpserver.exceptions import ToolError

        agent = verified_agent_identity()
        raise ToolError(
            f"Hub-mediated filesystem access is denied for agent {agent!r} "
            "(explicit UA_AGENT_WORKSPACES DENY entry); use this identity's "
            "own native file tools instead."
        )
    if override is not None:
        return override
    return process_workspace_root()


def process_workspace_root() -> Path:
    """Hub root, preserving normal bindings while ignoring explicit DENY.

    Agent-facing ``fs_*`` / ``run_cmd`` tools keep using ``workspace_root()``,
    which raises for a ``UA_AGENT_WORKSPACES`` DENY identity. Hub-owned census
    (worktree survey, ``system_doctor``) must still inspect this host's disk
    when the Control Center operator is that denied identity — otherwise a
    read-only pane fails closed on filesystem policy that does not apply to it.
    """
    override = workspace_root_var.get()
    if isinstance(override, Path):
        return override
    return Path(os.environ.get("UA_WORKSPACE") or str(default_workspace())).expanduser()


# Installation-owned project data. These views do not widen fs_*/run_cmd
# sandboxes; only internal, existence-checked checkout routing consumes them.
_KNOWN_PROJECTS = _projects.ProjectIds()
PROJECT_REPO_DIRNAMES = _projects.ProjectField("dirname")
PROJECT_REPO_SEED = _projects.ProjectField("repo")


def project_registry() -> dict[str, dict[str, str]]:
    return _projects.registry()


def project_default_domain(project: str) -> str:
    return project_registry().get(project, {}).get("domain", "hobby")


def task_list_project_key(project_name: str) -> str:
    """Map a checkout/git folder name to the ``task_list`` project slug when known."""
    lower = project_name.lower()
    dash = lower.replace("_", "-")
    if dash in PROJECT_REPO_DIRNAMES:
        return dash
    for stream, dirname in PROJECT_REPO_DIRNAMES.items():
        if lower == dirname.lower():
            return stream
    return project_name


def project_repo_strict_worktree_check() -> bool:
    """Opt-in strict mode (task #1425): refuse, rather than warn, when a
    task-driven worktree would be cut from a project repo that has no
    binding or whose checkout's origin doesn't match its bound repo.

    Off by default -- most project streams have no binding yet, and a hard
    refusal there would break today's worktree creation for them. See
    `project_bindings.check_worktree_binding`.
    """
    return os.environ.get("UA_PROJECT_REPO_STRICT", "").strip().lower() in ("1", "true", "yes")


def dev_root() -> Path:
    """Root of this host's ~/dev checkouts (override for tests via UA_DEV_ROOT)."""
    return Path(os.environ.get("UA_DEV_ROOT", "~/dev")).expanduser()


def project_repo_path(project: str | None) -> Path | None:
    """Resolved ~/dev checkout path for a known, currently-checked-out project.

    None for an unrecognized project, or one whose directory does not exist on
    this host (existence-checked, mirroring webui.project_registry). Task #371.
    """
    if project not in _KNOWN_PROJECTS:
        return None
    dirname = PROJECT_REPO_DIRNAMES.get(project, project)
    root = dev_root().resolve()
    path = (root / dirname).resolve()
    return (
        path
        if not (root / dirname).is_symlink() and path.is_relative_to(root) and path.is_dir()
        else None
    )


def project_repo_roots() -> list[Path]:
    """Every known project's resolved repo path that exists on this host."""
    roots = []
    for project in _KNOWN_PROJECTS:
        path = project_repo_path(project)
        if path is not None:
            roots.append(path)
    return roots


def data_home() -> Path:
    return Path(os.environ.get("UA_HOME", "~/.unified_agent")).expanduser()


def db_path() -> Path:
    return data_home() / "memory.db"


def org_partition_enabled() -> bool:
    """Whether this operator allows the org_id sentinel backfill (task #1433).

    Off by default. It permits ``unified-agent db org-partition --apply`` and
    nothing else: a store's marker, not this flag, decides how it is read, so
    every process sharing the store agrees. See docs/architecture/tenancy.md.
    """
    return os.environ.get("UA_ORG_PARTITION", "").strip() == "1"


def backup_dir() -> Path:
    """Directory for local, complete SQLite backups of ``memory.db``.

    Kept under UA_HOME by default so backups follow an explicitly relocated
    installation, while a dedicated path remains available for an operator's
    encrypted or separately-mounted volume.
    """
    return Path(os.environ.get("UA_BACKUP_DIR", str(data_home() / "backups"))).expanduser()


def backup_gcs_uri() -> str | None:
    """Optional ``gs://bucket/prefix`` for offsite copies after a local backup.

    When unset or empty, ``create_backup`` stays local-only. Local snapshots
    remain the source of truth; GCS failures must not fail the local path.
    """
    raw = os.environ.get("UA_BACKUP_GCS_URI", "").strip()
    return raw or None


def backup_s3_uri() -> str | None:
    """Optional ``s3://bucket/prefix`` for S3-compatible offsite copies (task #2585).

    Cloudflare R2 in the hobby cloud setup (#2584). Unset keeps backups local.
    """
    raw = os.environ.get("UA_BACKUP_S3_URI", "").strip()
    return raw or None


def backup_s3_endpoint() -> str | None:
    """``https://`` origin of the S3-compatible store, e.g. the R2 account endpoint."""
    raw = os.environ.get("UA_BACKUP_S3_ENDPOINT", "").strip()
    return raw or None


def backup_s3_region() -> str:
    """SigV4 signing region; R2 expects ``auto``, AWS the bucket's region."""
    return os.environ.get("UA_BACKUP_S3_REGION", "").strip() or "auto"


def backup_s3_credentials() -> tuple[str | None, str | None]:
    """Access key id and secret for the offsite store. Never log either."""
    key_id = os.environ.get("UA_BACKUP_S3_ACCESS_KEY_ID", "").strip() or None
    secret = os.environ.get("UA_BACKUP_S3_SECRET_ACCESS_KEY", "").strip() or None
    return key_id, secret


def backup_local_keep() -> tuple[int, int]:
    """Local daily / weekly snapshot generations to keep (default 7 / 4).

    ``UA_BACKUP_KEEP_DAILY`` is at least 1, so the newest snapshot always
    survives; ``UA_BACKUP_KEEP_WEEKLY`` may be 0. An unparsable value falls
    back to the default rather than deleting more than intended.
    """

    def _read(name: str, default: int, minimum: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(minimum, int(raw))
        except ValueError:
            return default

    return _read("UA_BACKUP_KEEP_DAILY", 7, 1), _read("UA_BACKUP_KEEP_WEEKLY", 4, 0)


def gcp_project() -> str | None:
    """Explicit GCP project id; unset disables ambient project selection."""
    return os.environ.get("UA_GCP_PROJECT", "").strip() or None


def gcp_worker() -> str | None:
    """Explicit VM name eligible for bounded cloud health and recovery."""
    return os.environ.get("UA_GCP_WORKER", "").strip() or None


def gcloud_bin() -> str:
    """Path to gcloud executable (prefers ~/google-cloud-sdk/bin/gcloud if present)."""
    explicit = os.environ.get("UA_GCLOUD_BIN", "").strip()
    if explicit:
        return explicit
    sdk_default = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"
    if sdk_default.is_file() and os.access(sdk_default, os.X_OK):
        return str(sdk_default)
    return "gcloud"


def shell_allowlist() -> set[str]:
    """Programs ``run_cmd`` may start.

    When ``UA_SHELL_ALLOWLIST`` is set, it replaces ``DEFAULT_SHELL_ALLOWLIST``.
    Keep a default program by naming it in that colon-separated value. An unset
    or empty variable uses the default list; a value that names no program,
    such as ``:``, denies every command.
    """
    raw = os.environ.get("UA_SHELL_ALLOWLIST")
    if raw:
        return set(filter(None, raw.split(":")))
    return set(DEFAULT_SHELL_ALLOWLIST.split())


def configured_toolsets() -> frozenset[str] | None:
    """Hub-wide toolsets to advertise in the MCP manifest.

    ``UA_TOOLSETS`` is a colon-separated list (``core:tasks``). Unset, blank,
    or a value with no names returns None, which means every toolset. ``core``
    lists nine tools. ``profile_status`` and ``memory_search`` stay listed on
    every selection, including ``memory`` and ``admin``. This chooses listing
    only; it does not authorize or block a tool call.
    """
    raw = os.environ.get("UA_TOOLSETS")
    if raw is None or not raw.strip():
        return None
    names = frozenset(part.strip() for part in raw.split(":") if part.strip())
    if not names:
        return None
    return names


def transport() -> str:
    t = os.environ.get("UA_TRANSPORT", "stdio")
    if t not in ("stdio", "http"):
        raise ValueError(f"UA_TRANSPORT must be 'stdio' or 'http', got {t!r}")
    return t


def http_host() -> str:
    return os.environ.get("UA_HTTP_HOST", "127.0.0.1")


def http_port() -> int:
    raw = os.environ.get("UA_HTTP_PORT", "8765")
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"UA_HTTP_PORT must be an integer, got {raw!r}") from None


def is_primary_client(agent_label: str | None) -> bool:
    """Return whether an identity names a remote cockpit/product client."""
    label = (agent_label or "").strip()
    return label.startswith(("Desktop_", "Laptop_"))


def http_hub_url(agent_label: str | None = None, explicit: str | None = None) -> str:
    """Resolve the MCP hub URL without guessing for primary clients.

    ``UA_HTTP_HOST``/``UA_HTTP_PORT`` describe where a local hub binds. They
    are not a safe default for Desktop/Laptop clients, which must name the hub
    they intend to use. Server-local processes and explicitly configured
    satellites retain the loopback default.
    """
    configured = (explicit or os.environ.get("UA_HTTP_HUB_URL", "")).strip()
    if configured:
        return configured
    if is_primary_client(agent_label):
        label = (agent_label or "").strip() or "<unknown>"
        raise RuntimeError(
            f"UA_HTTP_HUB_URL is required for primary client {label!r}; set it to the "
            "canonical hub's full MCP URL ending in /mcp. UA_HTTP_HOST/UA_HTTP_PORT "
            "only describe a local server bind; use an explicit UA_HTTP_HUB_URL for "
            "an isolated satellite."
        )
    return f"http://{http_host()}:{http_port()}/mcp"


def http_allowed_hosts() -> list[str]:
    """Comma-separated Host-header allowlist (entries may carry ports, so not colon-separated).

    Non-empty turns on DNS-rebinding protection; use 'name:*' to allow any port.
    """
    raw = os.environ.get("UA_HTTP_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def mcp_session_idle_timeout_configured() -> bool:
    """Whether ``UA_MCP_SESSION_IDLE_TIMEOUT_S`` carries a non-empty value.

    Callers building the streamable-HTTP kwargs dict must check this first:
    the key should be omitted entirely when unset, not set to a parsed
    default, so the MCP SDK's own default (``DEFAULT_SESSION_IDLE_TIMEOUT``,
    1800s / 30min, in the installed ``mcp/server/streamable_http_manager.py``)
    applies unchanged.
    """
    return bool(os.environ.get("UA_MCP_SESSION_IDLE_TIMEOUT_S", "").strip())


def mcp_session_idle_timeout_s() -> float | None:
    """Resolve and validate ``UA_MCP_SESSION_IDLE_TIMEOUT_S`` (task #1298).

    Stateful streamable-HTTP MCP sessions are reaped after an idle period —
    the SDK's ``StreamableHTTPSessionManager`` default is 1800s (30 minutes)
    with no HTTP request in flight — which drops 24/7 clients (hermes gateway
    symptom: ``MCPError: Session not found`` / ``Session termination failed:
    404`` every ~20-90 min). This knob lets an operator raise or disable that
    timeout without changing SDK defaults for everyone else.

    Only meaningful when :func:`mcp_session_idle_timeout_configured` is
    ``True``; call that first. Semantics, cited against the installed SDK
    (``.venv/lib/python*/site-packages/mcp/server/streamable_http_manager.py``):

    - ``StreamableHTTPSessionManager.__init__`` (same file, lines 98-99)
      rejects ``session_idle_timeout`` outright unless it is ``None`` or a
      finite number ``> 0`` — passing the literal ``0`` raises
      ``ValueError: session_idle_timeout must be a positive, finite number
      of seconds``. ``None`` is the SDK's real "disable" value (docstring,
      lines 76-77: "None disables the timeout so sessions live until the
      client deletes them or the manager shuts down").
    - So this function treats an ``UA_MCP_SESSION_IDLE_TIMEOUT_S=0`` input as
      the conventional "disable" spelling and returns ``None`` — the value
      that actually disables the timeout in the SDK — rather than forwarding
      a literal ``0`` the SDK would reject at startup.
    - A positive integer is returned unchanged, in seconds.
    - Negative or non-numeric input raises ``ValueError`` here, loudly, at
      startup — never silently ignored or clamped.
    """
    raw = os.environ.get("UA_MCP_SESSION_IDLE_TIMEOUT_S", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"UA_MCP_SESSION_IDLE_TIMEOUT_S must be an integer number of seconds, got {raw!r}"
        ) from None
    if value < 0:
        raise ValueError(
            "UA_MCP_SESSION_IDLE_TIMEOUT_S must be a non-negative integer "
            f"number of seconds (0 disables the idle timeout), got {value}"
        )
    return value if value > 0 else None


HTTP_GRACEFUL_SHUTDOWN_DEFAULT_S = 10
# The Mac hub plist gives launchd ExitTimeOut 30; leave headroom for the
# shutdown that follows the grace period, or launchd SIGKILLs it again.
HTTP_GRACEFUL_SHUTDOWN_MAX_S = 25


def http_graceful_shutdown_s() -> int:
    """Seconds the HTTP hub waits for open connections on shutdown (task #1812).

    ``UA_HTTP_GRACEFUL_SHUTDOWN_S``; default 10. Streamable-HTTP MCP clients
    hold long-lived streams open, so an unbounded uvicorn shutdown never
    finishes on its own: launchd then SIGKILLs the hub after its ExitTimeOut
    and the hub's cleanup never runs. After this many seconds uvicorn cancels
    the remaining connections and runs the normal shutdown path. Keep it below
    the service manager's stop timeout: values above
    ``HTTP_GRACEFUL_SHUTDOWN_MAX_S`` (25, under the hub plist's ExitTimeOut 30)
    are refused.
    """
    raw = os.environ.get("UA_HTTP_GRACEFUL_SHUTDOWN_S", "").strip()
    if not raw:
        return HTTP_GRACEFUL_SHUTDOWN_DEFAULT_S
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"UA_HTTP_GRACEFUL_SHUTDOWN_S must be an integer number of seconds, got {raw!r}"
        ) from None
    if not 1 <= value <= HTTP_GRACEFUL_SHUTDOWN_MAX_S:
        raise ValueError(
            f"UA_HTTP_GRACEFUL_SHUTDOWN_S must be 1-{HTTP_GRACEFUL_SHUTDOWN_MAX_S} seconds "
            f"(below the hub plist's ExitTimeOut 30), got {value}"
        )
    return value


def http_proxy_headers() -> bool:
    """Whether the TCP listener honours ``X-Forwarded-*`` (task #1892).

    Off by default — ``scope["client"]`` is then always the real socket peer,
    so a loopback process cannot claim a tailnet address by sending
    ``X-Forwarded-For``. ``UA_HTTP_PROXY_HEADERS=1`` (also ``on``/``true``/
    ``yes``) restores uvicorn's connecting-IP trust — the pre-#1892 behaviour —
    for peers matching :func:`http_forwarded_allow_ips`. Any matching peer can
    assert any client address, so this exists only as an escape hatch for a
    hub behind a local reverse proxy; prefer
    :func:`http_trusted_proxy_socket`, which confines the trust to a private
    listener.
    """
    raw = os.environ.get("UA_HTTP_PROXY_HEADERS", "").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def http_forwarded_allow_ips() -> str:
    """TCP peers whose ``X-Forwarded-*`` is honoured (``UA_HTTP_FORWARDED_ALLOW_IPS``).

    Comma-separated IPs or CIDRs, or ``*`` for every peer. Default
    ``127.0.0.1`` — uvicorn's own ``forwarded_allow_ips`` default, so the
    :func:`http_proxy_headers` escape hatch reproduces exactly what the old
    implicit trust allowed. Inert unless :func:`http_proxy_headers` is on.
    """
    return os.environ.get("UA_HTTP_FORWARDED_ALLOW_IPS", "").strip() or "127.0.0.1"


def http_trusted_proxy_socket() -> Path | None:
    """Unix socket for the trusted-proxy listener (``UA_HTTP_PROXY_SOCKET``, task #1892).

    Set = the hub binds a second listener at this absolute path, owner-only
    (``0600``), and honours ``X-Forwarded-*`` there — the listener a trusted
    local reverse proxy such as Tailscale Serve is pointed at
    (``tailscale serve unix:<path>``). The TCP listener never trusts forwarded
    headers unless :func:`http_proxy_headers` is explicitly on. Unset = no
    second listener and no trusted path at all. Relative paths and paths too
    long for the 104-byte macOS ``sun_path`` limit are refused.
    """
    raw = os.environ.get("UA_HTTP_PROXY_SOCKET", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"UA_HTTP_PROXY_SOCKET must be an absolute path, got {raw!r}")
    if len(os.fsencode(path)) >= 104:
        raise ValueError(
            f"UA_HTTP_PROXY_SOCKET exceeds the 104-byte unix socket path limit: {raw!r}"
        )
    return path


def http_public_hosts() -> set[str]:
    """Host headers reachable from the public internet (``UA_HTTP_PUBLIC_HOSTS``).

    Colon-separated, lowercased, port stripped. Empty by default: nothing is
    treated as publicly exposed. When set (the Grok Bot / Tailscale Funnel case,
    task #859), a request whose Host matches one of these is served ONLY the MCP
    and OAuth endpoints — ``/ui``, ``/api/*``, ``/healthz`` and everything else
    return a plain 404 before auth resolution, so a Funnel misconfiguration
    cannot leak the control-center UI. Each entry must also appear in
    ``UA_HTTP_ALLOWED_HOSTS`` or the request is rejected upstream.
    """
    raw = os.environ.get("UA_HTTP_PUBLIC_HOSTS", "")
    return {h.strip().lower().split(":", 1)[0] for h in raw.split(":") if h.strip()}


def http_allowed_origins() -> list[str]:
    """Browser Origin allowlist for the hub (MCP transport security + the web UI).

    Historically this was hardcoded to ["null"], which admits only file:// pages.
    The default is now the hub's own origin plus every UA_HTTP_ALLOWED_HOSTS
    entry (a host we accept a Host header from is a host we can accept a page
    from). ``null`` is not in that default: sandboxed iframes send
    ``Origin: null``. An operator who still needs that origin sets
    ``UA_HTTP_ALLOWED_ORIGINS=null,...`` explicitly; a non-empty value replaces
    the default entirely.
    """
    raw = os.environ.get("UA_HTTP_ALLOWED_ORIGINS", "")
    if raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]

    origins = [f"http://{http_host()}:{http_port()}"]
    for host in http_allowed_hosts():
        # 'name:*' means any port; Origin match is exact, so emit the wildcard form
        # and let the caller's matcher handle it (see webui.auth.origin_allowed).
        origins.append(f"http://{host}")
        origins.append(f"https://{host}")
    # dict.fromkeys de-dupes while preserving order
    return list(dict.fromkeys(origins))


# Once-per-process migration warnings. Startup cannot emit these (server.py and
# ensure_dirs belong to other changes); the first call to enforce_auth() or
# approval_gated_tools() prints each distinct line at most once.
_POSTURE_WARNED: set[str] = set()
_POSTURE_WARN_LOCK = threading.Lock()
_ENV_ON = frozenset({"1", "true", "yes"})
_ENV_OFF = frozenset({"0", "false", "no"})


def _env_token(name: str) -> str | None:
    """Stripped lowercase value, or None when the variable is unset or blank."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    token = raw.strip().lower()
    return token or None


def _env_on(name: str) -> bool:
    return _env_token(name) in _ENV_ON


def _env_off(name: str) -> bool:
    return _env_token(name) in _ENV_OFF


# Words that switch an on-by-default env flag off (case-insensitive, trimmed).
OFF_WORDS = frozenset({"0", "false", "no", "off"})


def env_flag_off(name: str, default: str = "1") -> bool:
    """True when env var ``name`` (or ``default`` if unset) is an off word.

    Shared by the on-by-default flags (``UA_REVIEW_TRUST_CI``,
    ``UA_POST_MERGE_VERIFY``) so they all accept ``0``/``false``/``no``/``off``.
    """
    return os.environ.get(name, default).strip().lower() in OFF_WORDS


def review_head_check_enabled() -> bool:
    """Whether the Approval Ledger poll publishes ``unified-agent/review-head``
    (task #2320). On by default; ``UA_REVIEW_HEAD_CHECK=0`` stops new posts and
    leaves what is already on GitHub in place."""
    return not env_flag_off("UA_REVIEW_HEAD_CHECK")


def _warn_posture_once(key: str, message: str) -> None:
    with _POSTURE_WARN_LOCK:
        if key in _POSTURE_WARNED:
            return
        _POSTURE_WARNED.add(key)
    print(message, file=sys.stderr)


def warn_legacy_open_posture() -> None:
    """Loud once-per-process notice that warn-mode was requested on purpose."""
    _warn_posture_once(
        "legacy-open-auth",
        "ua-audit: [Warning] UA_LEGACY_OPEN_POSTURE=1: authentication is warn-mode "
        "on purpose. Unauthenticated requests are accepted and anonymous prod "
        "actions are allowed. Unset UA_LEGACY_OPEN_POSTURE to restore the safer "
        "HTTP default.",
    )


def warn_stdio_identity_posture() -> None:
    """Once-per-process notice that stdio does not enforce identity gates.

    ``UA_TRUST_STDIO=1`` acknowledges local trust and replaces the warning with
    a single acknowledgement. A repeat call stays silent either way.
    """
    if _env_on("UA_TRUST_STDIO"):
        _warn_posture_once(
            "stdio-identity",
            "ua-audit: [info] UA_TRUST_STDIO=1 acknowledged: stdio does not "
            "enforce identity gates (local trust).",
        )
        return
    _warn_posture_once(
        "stdio-identity",
        "ua-audit: [Warning] stdio transport does not enforce identity gates. "
        "Set UA_TRUST_STDIO=1 to acknowledge local trust, or "
        "UA_REQUIRE_CALLER_IDENTITY=1 to deny anonymous prod actions.",
    )


def legacy_open_posture() -> bool:
    """True when ``UA_LEGACY_OPEN_POSTURE`` is 1/true/yes."""
    return _env_on("UA_LEGACY_OPEN_POSTURE")


def require_caller_identity() -> bool:
    """True when ``UA_REQUIRE_CALLER_IDENTITY`` is 1/true/yes."""
    return _env_on("UA_REQUIRE_CALLER_IDENTITY")


def anonymous_prod_action_open() -> bool:
    """Whether an unidentified caller may skip the prod-action gate.

    HTTP denies. Stdio skips unless ``UA_REQUIRE_CALLER_IDENTITY`` is on.
    ``UA_LEGACY_OPEN_POSTURE=1`` skips on every transport. Skipping emits the
    matching once-per-process migration warning.
    """
    if legacy_open_posture():
        warn_legacy_open_posture()
        return True
    if transport() != "http" and not require_caller_identity():
        warn_stdio_identity_posture()
        return True
    return False


def enforce_auth() -> bool:
    """Whether an unauthenticated hub request is rejected (vs. warned about).

    An explicit ``UA_ENFORCE_AUTH`` wins: ``1``/``true``/``yes`` on,
    ``0``/``false``/``no`` off (any other explicit token stays off, matching
    the historical ``== "1"`` check). When the variable is unset:

    - ``UA_LEGACY_OPEN_POSTURE=1`` restores warn-mode and warns on every boot.
    - stdio stays open so satellites keep working, with one migration warning
      (``UA_TRUST_STDIO=1`` acknowledges it).
    - HTTP enforces, with one info line naming ``UA_LEGACY_OPEN_POSTURE``.

    Was read inline in server.py's middleware; lifted here because the web UI has to
    report it (a control plane must be able to say whether it is actually protected)
    and two places parsing the same env var is how they drift.
    """
    token = _env_token("UA_ENFORCE_AUTH")
    if token is not None:
        return token in _ENV_ON

    if legacy_open_posture():
        warn_legacy_open_posture()
        return False

    if transport() != "http":
        warn_stdio_identity_posture()
        return False

    _warn_posture_once(
        "http-enforce-default",
        "ua-audit: [info] UA_ENFORCE_AUTH is unset with UA_TRANSPORT=http; "
        "enforcing authentication. Set UA_LEGACY_OPEN_POSTURE=1 to restore "
        "warn-mode for a migration window.",
    )
    return True


def enforce_auth_from_settings(
    enforce_raw: str | None,
    transport_raw: str | None,
    legacy_raw: str | None,
) -> bool:
    """``enforce_auth`` for an explicit settings triple, with no env read and no warning.

    A blank ``enforce_raw`` is unset. A blank ``transport_raw`` is stdio.
    ``system_doctor`` uses this so the hub unit is not confused with the
    process that invoked the doctor.
    """
    token = (enforce_raw or "").strip().lower()
    if token:
        return token in _ENV_ON
    if (legacy_raw or "").strip().lower() in _ENV_ON:
        return False
    return (transport_raw or "stdio").strip().lower() == "http"


# --- Canonical deployment modes (task #1422, Parity 17) -----------------------
DEPLOYMENT_MODE_LOCAL_TRUSTED = "local_trusted"
DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE = "authenticated_private"
DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC = "authenticated_public"

CANONICAL_DEPLOYMENT_MODES: tuple[str, ...] = (
    DEPLOYMENT_MODE_LOCAL_TRUSTED,
    DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE,
    DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC,
)

_DEPLOYMENT_MODE_ALIASES: dict[str, str] = {
    "local_trusted": DEPLOYMENT_MODE_LOCAL_TRUSTED,
    "local-trusted": DEPLOYMENT_MODE_LOCAL_TRUSTED,
    "local": DEPLOYMENT_MODE_LOCAL_TRUSTED,
    "trusted": DEPLOYMENT_MODE_LOCAL_TRUSTED,
    "authenticated_private": DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE,
    "authenticated-private": DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE,
    "authenticated+private": DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE,
    "private": DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE,
    "authenticated_public": DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC,
    "authenticated-public": DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC,
    "authenticated+public": DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC,
    "public": DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC,
}


class DeploymentModeError(ValueError):
    """Raised when UA_DEPLOYMENT_MODE is invalid or violates transport/auth invariants."""


def normalize_deployment_mode(raw: str | None) -> str | None:
    """Normalize raw deployment mode string to canonical token or None."""
    if not raw or not raw.strip():
        return None
    return _DEPLOYMENT_MODE_ALIASES.get(raw.strip().lower())


def deployment_mode_from_settings(
    mode_raw: str | None,
    enforce_raw: str | None,
    transport_raw: str | None,
    legacy_raw: str | None,
    public_raw: str | None = None,
    public_url_raw: str | None = None,
    public_hosts_raw: str | None = None,
) -> tuple[str, str]:
    """Resolve deployment mode and attribution gate without reading ambient os.environ.

    Returns:
        (resolved_mode, gate_description)
    """
    if mode_raw and mode_raw.strip():
        normalized = normalize_deployment_mode(mode_raw)
        if normalized is None:
            raise DeploymentModeError(
                f"invalid deployment mode {mode_raw!r}; must be one of {sorted(set(CANONICAL_DEPLOYMENT_MODES))}"
            )
        return normalized, "UA_DEPLOYMENT_MODE"

    enforce = enforce_auth_from_settings(enforce_raw, transport_raw, legacy_raw)
    is_public = (
        (public_raw or "").strip().lower() in _ENV_ON
        or bool((public_url_raw or "").strip())
        or bool((public_hosts_raw or "").strip())
    )

    if not enforce:
        return DEPLOYMENT_MODE_LOCAL_TRUSTED, "inferred (auth unforced / open posture)"
    if is_public:
        return DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC, "inferred (enforced auth + public ingress)"
    return DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE, "inferred (enforced auth + private transport)"


def has_configured_credentials() -> bool:
    """True if at least one credential leg is configured."""
    try:
        tokens = agent_tokens()
    except Exception:
        tokens = {}
    from . import identity_core

    try:
        workos_ready = bool(identity_core.build_hub_auth() is not None)
    except Exception:
        workos_ready = False

    return bool(tokens or mcp_oauth_enabled() or workos_ready)


def deployment_mode() -> str:
    """Canonical deployment mode for this instance.

    One of:
      - ``local_trusted``: single-workstation / unforced-auth / developer loopback
      - ``authenticated_private``: private-network (tailnet / VPC) with enforced authentication
      - ``authenticated_public``: public-origin / Internet-facing with enforced authentication

    Explicit ``UA_DEPLOYMENT_MODE`` wins. When unset, inferred from
    ``enforce_auth()`` and public ingress indicators.
    """
    mode_raw = os.environ.get("UA_DEPLOYMENT_MODE")
    if mode_raw and mode_raw.strip():
        normalized = normalize_deployment_mode(mode_raw)
        if normalized is None:
            raise DeploymentModeError(
                f"invalid UA_DEPLOYMENT_MODE={mode_raw!r}; must be one of {sorted(CANONICAL_DEPLOYMENT_MODES)}"
            )
        return normalized

    if not enforce_auth():
        return DEPLOYMENT_MODE_LOCAL_TRUSTED

    if (
        os.environ.get("UA_AUTHENTICATED_PUBLIC", "").strip().lower() in _ENV_ON
        or os.environ.get("UA_PUBLIC_URL", "").strip()
        or os.environ.get("UA_HTTP_PUBLIC_HOSTS", "").strip()
    ):
        return DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC

    return DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE


def _is_loopback_host(host: str) -> bool:
    cleaned = host.strip().lower()
    if cleaned in {"127.0.0.1", "::1"}:
        return True
    if cleaned == "localhost":
        try:
            infos = socket.getaddrinfo(cleaned, None)
            return bool(infos) and all(
                ipaddress.ip_address(info[4][0]).is_loopback for info in infos
            )
        except Exception:
            return False
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_deployment_mode(
    mode: str | None = None,
    *,
    enforce: bool | None = None,
    tokens_configured: bool | None = None,
    public: bool | None = None,
    public_url: str | None = None,
    public_hosts: str | None = None,
    transport: str | None = None,
    http_host: str | None = None,
) -> None:
    """Refuse mode/transport/auth mismatches fail-closed.

    Raises:
        DeploymentModeError: on any fatal configuration contradiction.
    """
    resolved_mode = mode or deployment_mode()
    if resolved_mode not in CANONICAL_DEPLOYMENT_MODES:
        raise DeploymentModeError(f"unknown deployment mode: {resolved_mode!r}")

    is_enforced = enforce if enforce is not None else enforce_auth()
    is_public = (
        public
        if public is not None
        else (os.environ.get("UA_AUTHENTICATED_PUBLIC", "").strip().lower() in _ENV_ON)
    )
    has_public_url = bool(
        (public_url if public_url is not None else os.environ.get("UA_PUBLIC_URL", "")).strip()
    )
    has_public_hosts = bool(
        (
            public_hosts if public_hosts is not None else os.environ.get("UA_HTTP_PUBLIC_HOSTS", "")
        ).strip()
    )
    resolved_transport = (
        (transport if transport is not None else globals()["transport"]()).strip().lower()
    )
    resolved_host = (
        (http_host if http_host is not None else globals()["http_host"]()).strip().lower()
    )

    if resolved_mode == DEPLOYMENT_MODE_LOCAL_TRUSTED:
        if is_public or has_public_url or has_public_hosts:
            raise DeploymentModeError(
                "local_trusted mode cannot be combined with public ingress "
                "(UA_AUTHENTICATED_PUBLIC, UA_PUBLIC_URL, or UA_HTTP_PUBLIC_HOSTS is configured)"
            )
        if resolved_transport == "http" and (
            resolved_host in {"0.0.0.0", "::", "*", ""} or not _is_loopback_host(resolved_host)
        ):
            raise DeploymentModeError(
                f"local_trusted mode forbids wildcard or non-loopback HTTP host ({resolved_host!r}); "
                "bind to 127.0.0.1 or enforce authentication"
            )

    elif resolved_mode == DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC:
        if not is_enforced:
            raise DeploymentModeError(
                "authenticated_public mode refuses unauthenticated access; "
                "authentication must be enforced (UA_ENFORCE_AUTH cannot be disabled)"
            )
        creds_configured = (
            tokens_configured if tokens_configured is not None else has_configured_credentials()
        )
        if creds_configured is False:
            raise DeploymentModeError(
                "authenticated_public mode requires configured authentication credentials (none found)"
            )

    elif resolved_mode == DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE:
        if not is_enforced:
            raise DeploymentModeError(
                "authenticated_private mode requires enforced authentication (UA_ENFORCE_AUTH=1)"
            )


DEFAULT_MCP_OAUTH_AGENT = "Remote_Agent"
DEFAULT_MCP_OAUTH_TTL_S = 7 * 24 * 3600


def mcp_oauth_flag() -> bool:
    """Raw ``UA_MCP_OAUTH=1`` opt-in. Not enough on its own — see ``mcp_oauth_enabled``."""
    return os.environ.get("UA_MCP_OAUTH", "0") == "1"


def mcp_oauth_issuer() -> str | None:
    """Public origin remote MCP clients will call. No trailing slash."""
    raw = os.environ.get("UA_MCP_OAUTH_ISSUER", "").strip().rstrip("/")
    return raw or None


def public_origin() -> str | None:
    """The hub's public origin for browser redirects, or None (task #2114).

    ``UA_MCP_OAUTH_ISSUER`` first, as the OAuth approver uses it, then
    ``UA_PUBLIC_URL`` (the hosted entrypoint sets it from
    ``RAILWAY_PUBLIC_DOMAIN``). Only an absolute http(s) URL counts. Request
    Host headers are never consulted: a redirect URI built from them could be
    steered by whoever sends the request.
    """
    from urllib.parse import urlsplit

    for raw in (mcp_oauth_issuer() or "", os.environ.get("UA_PUBLIC_URL", "")):
        value = raw.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme in ("https", "http") and parts.netloc and not parts.query:
            return value
    return None


def hosted() -> bool:
    """True on a hosted platform: Railway (``RAILWAY_ENVIRONMENT_ID``) or ``UA_HOSTED=1``.

    The same test ``deploy/docker/entrypoint.sh`` uses.
    """
    return bool(os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()) or (
        os.environ.get("UA_HOSTED", "").strip() == "1"
    )


TAILNET_PEER_MODES = ("off", "whois")


def tailnet_peer_mode() -> str:
    """``UA_TAILNET_PEER_MODE``: ``whois`` resolves tailnet peers, ``off`` never does.

    Default ``whois`` on a normal host and always ``off`` when :func:`hosted` (task
    #2114). Railway's edge proxy connects from 100.64.0.0/10, the Tailscale
    CGNAT range, so on a hosted hub a CGNAT peer address says nothing about a
    tailnet node and must not be looked up. An unknown value is treated as
    ``off``: this setting only ever removes provenance, so failing toward it
    is the safe side.
    """
    if hosted():
        # Installing tailscaled does not change the edge proxy's provenance.
        return "off"
    raw = os.environ.get("UA_TAILNET_PEER_MODE", "").strip().lower()
    if not raw:
        return "whois"
    return raw if raw in TAILNET_PEER_MODES else "off"


def mcp_oauth_passphrase() -> str:
    """Shared consent secret. Approves only when the fallback flag is on (task #1322)."""
    return os.environ.get("UA_MCP_OAUTH_PASSPHRASE", "")


def mcp_oauth_passphrase_fallback() -> bool:
    """``UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1`` lets the shared passphrase approve.

    Off by default (task #1322, J3): consent and device approval need a verified
    human (WorkOS session, or tailnet login plus passkey). A passphrase approval
    is recorded ``human.verified=false``. Hosted public hubs never enable this
    fallback, even when a legacy environment flag still asks for it (#2116).
    """
    return not hosted() and (os.environ.get("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "0").strip() == "1")


def mcp_oauth_approver_logins() -> list[str]:
    """Tailnet login names (``tailscale whois`` UserProfile.LoginName) of the
    humans who may approve OAuth consent/device sign-in (task #1322). Lowercased."""
    raw = os.environ.get("UA_MCP_OAUTH_APPROVER_LOGINS", "")
    return sorted(
        {part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()}
    )


def mcp_oauth_passkey_rp_id() -> str | None:
    """WebAuthn relying-party id for approver passkeys: the issuer host by default.

    ``UA_MCP_OAUTH_PASSKEY_RP_ID`` may name a parent domain of the issuer host
    (WebAuthn's registrable-suffix rule); anything else is ignored.
    """
    from urllib.parse import urlparse

    issuer = mcp_oauth_issuer()
    host = (urlparse(issuer).hostname or "").lower() if issuer else ""
    if not host:
        return None
    raw = os.environ.get("UA_MCP_OAUTH_PASSKEY_RP_ID", "").strip().lower().rstrip(".")
    if raw and (host == raw or host.endswith("." + raw)):
        return raw
    return host


def mcp_oauth_human_approver_configured() -> bool:
    """True when a verified-human approval path exists (WorkOS leg or tailnet logins)."""
    if mcp_oauth_approver_logins():
        return True
    try:
        from . import identity_core

        return identity_core.hub_auth() is not None
    except Exception:
        return False


def mcp_oauth_agent() -> str:
    """Hub identity minted OAuth access tokens authenticate as by default."""
    raw = os.environ.get("UA_MCP_OAUTH_AGENT", "").strip()
    if raw:
        return raw
    roster = agent_labels()
    if roster and DEFAULT_MCP_OAUTH_AGENT not in roster:
        if "Desktop_Agent1" in roster:
            return "Desktop_Agent1"
        return sorted(roster)[0]
    return DEFAULT_MCP_OAUTH_AGENT


def mcp_oauth_ttl_seconds() -> int:
    """Access-token lifetime. Default one week so remote MCP agents are not re-keyed every session."""
    raw = os.environ.get("UA_MCP_OAUTH_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MCP_OAUTH_TTL_S
    try:
        val = int(raw)
        return val if val > 0 else DEFAULT_MCP_OAUTH_TTL_S
    except ValueError:
        return DEFAULT_MCP_OAUTH_TTL_S


def mcp_oauth_cimd_enabled() -> bool:
    """Accept URL client_ids (Client ID Metadata Documents, task #1323). Opt-in:
    it lets an unauthenticated /authorize make the hub fetch a public URL."""
    return os.environ.get("UA_MCP_OAUTH_CIMD", "").strip() == "1"


def mcp_oauth_cimd_allow() -> list[str]:
    """Hosts whose Client ID Metadata Documents the hub may fetch (task #1323).

    Comma-separated; a leading ``.`` also admits subdomains. Empty (the default)
    admits any public HTTPS host — the SSRF guard still refuses private ones.
    """
    raw = os.environ.get("UA_MCP_OAUTH_CIMD_ALLOW", "")
    return [h.strip().lower() for h in raw.replace(";", ",").split(",") if h.strip()]


def mcp_oauth_client_credentials(raw: str | None = None) -> list[tuple[str, str, str]]:
    """Parse ``UA_MCP_OAUTH_CLIENT_CREDENTIALS`` into ``(client_id, secret, agent)`` triples.

    Non-interactive cloud-bot path (task #870). Entries are comma/newline-separated
    ``client_id|client_secret|agent_label`` — pipe separators so secrets may contain
    colons. Each agent must be a known hub label; unknown labels and malformed
    rows are skipped. Duplicate ``client_id`` values fail closed (that id is
    dropped entirely). Maps onto existing profiles only — no new powers.
    """
    value = (
        raw if raw is not None else os.environ.get("UA_MCP_OAUTH_CLIENT_CREDENTIALS", "")
    ).strip()
    if not value:
        return []
    if value.startswith("/") or value.startswith("~") or os.path.exists(value):
        try:
            path = Path(value).expanduser()
            if path.exists() and path.is_file():
                value = path.read_text().strip()
        except Exception:
            pass
    by_id: dict[str, tuple[str, str, str]] = {}
    conflicts: set[str] = set()
    for client_id, secret, agent in _client_credential_triples(value):
        canonical = canonical_agent_label(agent) or agent
        if not is_known_agent_label(canonical):
            continue
        if client_id in by_id and by_id[client_id] != (client_id, secret, canonical):
            conflicts.add(client_id)
            continue
        by_id[client_id] = (client_id, secret, canonical)
    for client_id in conflicts:
        by_id.pop(client_id, None)
    return list(by_id.values())


def _client_credential_triples(value: str) -> list[tuple[str, str, str]]:
    """Well-formed ``client_id|client_secret|agent`` entries, before label checks.

    The structural half of :func:`mcp_oauth_client_credentials`, shared with the
    operator-alias worker scan so both agree on which entries can authenticate.
    """
    entries = [line.strip() for line in value.replace("\n", ",").split(",") if line.strip()]
    triples: list[tuple[str, str, str]] = []
    for entry in entries:
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) == 3 and all(parts):
            triples.append((parts[0], parts[1], parts[2]))
    return triples


def _oauth_client_pins(value: str | None = None) -> list[tuple[str, str, str]]:
    """Well-formed ``agent:client_id:client_secret`` pins, before label checks.

    The structural half of :func:`mcp_oauth_clients`, shared with the
    operator-alias worker scan. ``None`` reads the environment. Raises
    ``ValueError`` like ``_parse_colon_pairs`` when a path-like value cannot be
    read.
    """
    pins: list[tuple[str, str, str]] = []
    for agent, details in _parse_colon_pairs("UA_MCP_OAUTH_CLIENTS", value):
        client_id, separator, secret = details.partition(":")
        if separator and client_id.strip() and secret.strip():
            pins.append((agent, client_id, secret))
    return pins


def mcp_oauth_clients(raw: str | None = None) -> dict[str, tuple[str, str]]:
    """Parse pinned OAuth client credentials as ``client_id: (agent, secret)``.

    Entries use ``agent:client_id:client_secret`` and may be comma/newline
    separated or stored in a file. Invalid agents and duplicate identities or
    client IDs are ignored so one ambiguous registration cannot authenticate.
    """
    value = raw if raw is not None else os.environ.get("UA_MCP_OAUTH_CLIENTS", "")
    if not value.strip():
        return {}

    candidates: list[tuple[str, str, str]] = []
    for agent, client_id, secret in _oauth_client_pins(value):
        canonical = canonical_agent_label(agent) or agent
        if not is_known_agent_label(canonical):
            continue
        candidates.append((canonical, client_id, secret))

    client_counts: dict[str, int] = {}
    identity_counts: dict[str, int] = {}
    for identity, client_id, _secret in candidates:
        client_counts[client_id] = client_counts.get(client_id, 0) + 1
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    return {
        client_id: (identity, secret)
        for identity, client_id, secret in candidates
        if client_counts[client_id] == 1 and identity_counts[identity] == 1
    }


def _mcp_oauth_issuer_ok(issuer: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(issuer)
    except ValueError:
        return False
    if parsed.fragment or parsed.query:
        return False
    try:
        # Accessing .port parses the netloc's port and raises ValueError for a
        # non-numeric or out-of-range (not 0-65535) value — urlparse() itself
        # does not validate it eagerly, so a malformed port (e.g. ":bad")
        # would otherwise sail through unnoticed (task #1514).
        port = parsed.port
    except ValueError:
        return False
    if port == 0:
        return False
    try:
        host = parsed.hostname or ""
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(host)
    return parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}


def mcp_oauth_enabled() -> bool:
    """True only when the flag, issuer, an approver, and agent identity are all valid.

    Partial config stays off (fail closed) so a public tunnel cannot come up as an
    unauthenticated MCP endpoint. The approver is a verified-human path (task
    #1322) or the legacy passphrase. A passphrase alone keeps the server — and
    every token already issued — up, but without
    ``UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1`` it approves nothing new.
    """
    if not mcp_oauth_flag():
        return False
    issuer = mcp_oauth_issuer()
    if not issuer or not _mcp_oauth_issuer_ok(issuer):
        return False
    if not mcp_oauth_passphrase() and not mcp_oauth_human_approver_configured():
        return False
    return is_known_agent_label(mcp_oauth_agent())


def heartbeat_min_interval_s() -> float:
    """Floor between two registry writes for the same agent label (default 15s).

    The heartbeat fires on hot paths (every authenticated hub request). This
    throttle is what keeps that from becoming a write per request on a SQLite
    file already shared by the hub, executor, cron and TUI.
    """
    raw = os.environ.get("UA_HEARTBEAT_MIN_INTERVAL_S")
    if not raw:
        return 15.0
    try:
        val = float(raw)
        return val if val >= 0 else 15.0
    except ValueError:
        return 15.0


def _nonnegative_seconds_env(name: str, default: float) -> float:
    """A seconds setting where ``0`` means off; unset or malformed is ``default``."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if math.isfinite(val) and val >= 0 else default


DOCTOR_CACHE_DEFAULT_S = 120.0
DOCTOR_MAX_STALE_DEFAULT_S = 600.0


def doctor_cache_ttl_s() -> float:
    """Seconds one ``system_doctor`` report is served as fresh (default 120s, ``0`` disables).

    A report runs every probe (systemctl/launchctl, an executor round-trip, a
    KNN query, LLM metadata GETs, a git survey of every worktree), so a polled
    or concurrent caller must share one run instead of starting its own (task
    #1850). Past this age a report is still served, up to
    ``doctor_max_stale_s()``, while one background run replaces it (task
    #1897). The default keeps unforced full runs to about one per two minutes
    per hub under the Control Center's 60s poll; ``refresh=True`` runs at any
    time. ``0`` turns off reuse entirely,
    stale serving included; ``system_doctor(refresh=True)`` always runs now.
    """
    return _nonnegative_seconds_env("UA_DOCTOR_CACHE_S", DOCTOR_CACHE_DEFAULT_S)


def doctor_max_stale_s() -> float:
    """Oldest ``system_doctor`` report served without blocking (default 600s).

    Between ``doctor_cache_ttl_s()`` and this age a caller gets the last report
    at once, marked ``refreshing``, while exactly one background run replaces
    it (task #1897). Past it, the caller waits for a run rather than receive
    the old report (and gets the run's error if it fails), so the stale path
    never serves a report older than this. Exceptions to "never waits": a cold
    start, a report past this bound, and ``refresh=True``. The
    ``stale_worktrees`` row may also reflect a survey up to
    ``worktree_survey_cache_s()`` older, with its survey time in the detail.
    A value at or below the
    fresh TTL, or ``0``, turns stale serving off.
    """
    return _nonnegative_seconds_env("UA_DOCTOR_MAX_STALE_S", DOCTOR_MAX_STALE_DEFAULT_S)


def worktree_survey_cache_s() -> float:
    """Seconds one read-only worktree survey is reused (default 300s, ``0`` disables).

    The survey runs four or five git commands per worktree (status, rev-parse,
    ``merge-tree --write-tree``), which on a checkout with a few hundred
    worktrees costs several CPU-seconds. ``system_doctor``, its host snapshot,
    and the Control Center attention and executor panes all read it, so they
    share one survey (task #1897). Staleness here is days-scale task hygiene,
    not security state.
    """
    return _nonnegative_seconds_env("UA_WORKTREE_SURVEY_CACHE_S", 300.0)


def llm_probe_cache_ttl_s() -> float:
    """Seconds an LLM leg's metadata probe result is reused (default 60s, ``0`` disables).

    Failures are cached too, so a down endpoint (ollama on localhost, a
    remote provider) is not re-probed on every doctor run (task #1850).
    """
    return _nonnegative_seconds_env("UA_LLM_PROBE_CACHE_S", 60.0)


def backfill_batch_pause_s() -> float:
    """Pause between two embedding-backfill batches (default 0.05s, ``0`` disables).

    The start-up backfills run beside request handling on a small host; a
    short yield between batches keeps them from monopolizing the GIL and the
    SQLite writer while the hub is answering requests (task #1850).
    """
    return _nonnegative_seconds_env("UA_BACKFILL_BATCH_PAUSE_S", 0.05)


def session_capture_tail_chars() -> int:
    """Char budget for the transcript tail fed to the session-capture summarizer.

    Only the last N characters of the rendered transcript are summarized — a
    session end card is a pointer, not an archive. Override with
    UA_SESSION_CAPTURE_TAIL_CHARS (default 12000).
    """
    raw = os.environ.get("UA_SESSION_CAPTURE_TAIL_CHARS", "").strip()
    try:
        val = int(raw)
        return val if val > 0 else 12000
    except ValueError:
        return 12000


def session_capture_retention_days() -> int:
    """TTL in days for a session-capture memory note (default 45).

    Session detail decays fast; durable facts/decisions get promoted to
    long-lived notes by hand. Override with UA_SESSION_CAPTURE_RETENTION_DAYS.
    """
    raw = os.environ.get("UA_SESSION_CAPTURE_RETENTION_DAYS", "").strip()
    try:
        val = int(raw)
        return val if val > 0 else 45
    except ValueError:
        return 45


_DEFAULT_MEMORY_INJECT_CONFIDENCE_THRESHOLD = 0.7
_DEFAULT_MEMORY_INJECT_ITEM_CAP = 5
_DEFAULT_MEMORY_INJECT_TOKEN_CAP = 256


def memory_inject_confidence_threshold() -> float:
    """Minimum confidence (or salience, when confidence is unset) for packet injection.

    Automatic session-packet memory injection drops notes below this floor.
    Explicit ``memory_search`` is unchanged. Override with
    ``UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD`` (default 0.7, ECC instinct floor).
    Invalid or out-of-range values fall back to 0.7.
    """
    raw = os.environ.get("UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD", "0.7")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_INJECT_CONFIDENCE_THRESHOLD
    if not math.isfinite(val) or val < 0.0 or val > 1.0:
        return _DEFAULT_MEMORY_INJECT_CONFIDENCE_THRESHOLD
    return val


def memory_inject_item_cap() -> int:
    """Max memory hits attached to a session packet.

    Override with ``UA_MEMORY_INJECT_ITEM_CAP`` (default 5). Non-positive or
    unparseable values fall back to 5.
    """
    raw = os.environ.get("UA_MEMORY_INJECT_ITEM_CAP", "5")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_INJECT_ITEM_CAP
    return val if val >= 1 else _DEFAULT_MEMORY_INJECT_ITEM_CAP


def memory_inject_token_cap() -> int:
    """Whitespace-token budget for injected memory title+snippet text.

    Override with ``UA_MEMORY_INJECT_TOKEN_CAP`` (default 256). Non-positive or
    unparseable values fall back to 256.
    """
    raw = os.environ.get("UA_MEMORY_INJECT_TOKEN_CAP", "256")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_INJECT_TOKEN_CAP
    return val if val >= 1 else _DEFAULT_MEMORY_INJECT_TOKEN_CAP


# fastembed 0.8 TextEmbedding() default. Passed explicitly so a later
# fastembed release cannot change stored vectors without UA_EMBED_MODEL.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


def embed_model() -> str:
    """fastembed model id for semantic memory and symbol vectors.

    ``UA_EMBED_MODEL`` overrides. Empty or unset uses ``DEFAULT_EMBED_MODEL``.
    """
    raw = os.environ.get("UA_EMBED_MODEL", "").strip()
    return raw or DEFAULT_EMBED_MODEL


# Long notes are embedded as overlapping chunks (task #2182). On by default:
# a 30-question LongMemEval sample (5 per type, seed 2145) went from 26/30 to
# 27/30 strict recall@5, with no question scoring worse. Set
# UA_MEMORY_CHUNK_EMBEDDINGS=0 to keep the single truncated vector. See
# memory_chunks and docs/evals/longmemeval.md.
DEFAULT_MEMORY_CHUNK_EMBEDDINGS = True

_CHUNK_EMBEDDINGS_ON = frozenset({"1", "true", "on", "yes"})
_CHUNK_EMBEDDINGS_OFF = frozenset({"0", "false", "off", "no"})


def memory_chunk_embeddings() -> bool:
    """Whether a long memory note is stored as several overlapping vectors.

    ``UA_MEMORY_CHUNK_EMBEDDINGS`` accepts ``1``/``true``/``on``/``yes`` or
    ``0``/``false``/``off``/``no``. Anything else, including unset, uses
    :data:`DEFAULT_MEMORY_CHUNK_EMBEDDINGS`. Short notes always keep the
    single vector. The chunk width and cap live in ``memory_chunks``.
    """
    raw = os.environ.get("UA_MEMORY_CHUNK_EMBEDDINGS", "").strip().lower()
    if raw in _CHUNK_EMBEDDINGS_ON:
        return True
    if raw in _CHUNK_EMBEDDINGS_OFF:
        return False
    return DEFAULT_MEMORY_CHUNK_EMBEDDINGS


def agent_status_thresholds() -> tuple[float, float, float]:
    """(online, idle, stale) cutoffs in seconds for registry.derive_status.

    Beyond the third the agent reads 'offline'. Override with
    UA_AGENT_STATUS_THRESHOLDS="120,900,86400".
    """
    default = (120.0, 900.0, 86400.0)
    raw = os.environ.get("UA_AGENT_STATUS_THRESHOLDS", "").strip()
    if not raw:
        return default
    try:
        parts = [float(p.strip()) for p in raw.split(",") if p.strip()]
    except ValueError:
        return default
    if len(parts) != 3 or not (parts[0] < parts[1] < parts[2]):
        return default
    return (parts[0], parts[1], parts[2])


def session_abandon_minutes() -> float:
    """A 'running' session with no heartbeat for this long is reaped as 'abandoned'."""
    raw = os.environ.get("UA_SESSION_ABANDON_MINUTES")
    if not raw:
        return 30.0
    try:
        val = float(raw)
        return val if val > 0 else 30.0
    except ValueError:
        return 30.0


def session_live_minutes() -> float:
    """An open session not seen for this long is stale and not listed as live (task #1860).

    Independent of the reaper: ``session_abandon_minutes`` closes the row,
    this only decides whether a session is offered as a message target.
    """
    raw = os.environ.get("UA_SESSION_LIVE_MINUTES")
    if not raw:
        return 30.0
    try:
        val = float(raw)
    except ValueError:
        return 30.0
    # nan and inf compare false against every age, so they would hide (or never
    # expire) every session; a window past a year would overflow the cutoff date.
    if not math.isfinite(val) or val <= 0:
        return 30.0
    return min(val, 525600.0)


def friction_nudge_enabled() -> bool:
    """Whether session_end evaluates friction and may emit ONE learning-capture nudge.

    Default on. The nudge never writes memory or files a learning — it only
    suggests running propose_session_learnings (TeamAI emulation F1, task #914).
    """
    return os.environ.get("UA_FRICTION_NUDGE", "1") != "0"


def ready_dependent_notify_enabled() -> bool:
    """Whether a close tells each newly ready dependent's owner (task #2454).

    ``UA_READY_DEPENDENT_NOTIFY``, default on; ``0`` turns it off without a
    deploy. Off, a close records no readiness event and sends nothing.
    """
    return os.environ.get("UA_READY_DEPENDENT_NOTIFY", "1").strip() != "0"


def friction_score_threshold() -> int:
    """Minimum friction score to surface the nudge (UA_FRICTION_THRESHOLD, default 20).

    Calibrated (mirroring teamai-cli CONTRIBUTE_SMART_THRESHOLD) so ONE clear
    primary signal — a single interrupt, tool rejection, or correction, each
    worth 20 — crosses it, while the scale nudge (tool diversity + skill use,
    max ~10) never can. teamai recalibrated an earlier 60→35 scale before
    moving to this friction model; the boundary stays configurable.
    """
    raw = os.environ.get("UA_FRICTION_THRESHOLD")
    if not raw:
        return 20
    try:
        val = int(float(raw))
        return val if val > 0 else 20
    except ValueError:
        return 20


def friction_tool_call_gate() -> int:
    """Hard gate: a session with fewer than this many tool calls never nudges.

    UA_FRICTION_TOOL_GATE, default 15 (teamai-cli CONTRIBUTE_BASE_THRESHOLD). A
    one-command session the user rejected is not knowledge-base material no
    matter how much friction it shows.
    """
    raw = os.environ.get("UA_FRICTION_TOOL_GATE")
    if not raw:
        return 15
    try:
        val = int(float(raw))
        return val if val >= 0 else 15
    except ValueError:
        return 15


def event_bus_embed() -> bool:
    """Whether the hub process hosts the event bus itself (default on).

    Set UA_EVENT_BUS_EMBED=0 when running `unified-agent-eventbus` standalone;
    two owners racing the same socket is a misconfiguration, not a fallback.
    """
    return os.environ.get("UA_EVENT_BUS_EMBED", "1") != "0"


def ui_session_ttl_hours() -> float:
    """Lifetime of a /ui browser cookie session (default 12h)."""
    raw = os.environ.get("UA_UI_SESSION_TTL_HOURS")
    if not raw:
        return 12.0
    try:
        val = float(raw)
        return val if val > 0 else 12.0
    except ValueError:
        return 12.0


def ui_stream_poll_s() -> float:
    """Cursor-poll interval backing the /api/events/stream SSE endpoint."""
    raw = os.environ.get("UA_UI_STREAM_POLL_S")
    if not raw:
        return 1.0
    try:
        val = float(raw)
        return val if val > 0 else 1.0
    except ValueError:
        return 1.0


def ui_max_streams() -> int:
    """Concurrent SSE streams the hub will serve before returning 503.

    Each stream is a repeating DB reader; a browser left open in a dozen tabs
    must not be able to pile them up.
    """
    raw = os.environ.get("UA_UI_MAX_STREAMS")
    if not raw:
        return 8
    try:
        val = int(raw)
        return val if val > 0 else 8
    except ValueError:
        return 8


def operator_tz() -> str | None:
    """IANA zone for operator-facing day windows (UA_OPERATOR_TZ), or None for the host zone.

    Storage stays UTC. This only decides where an operator's "today" starts.
    """
    return os.environ.get("UA_OPERATOR_TZ") or None


def vault_root() -> Path | None:
    """Explicit Obsidian vault root; unset or empty disables vault tools."""
    raw = os.environ.get("UA_VAULT_PATH", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def vault_write_dirs() -> list[str]:
    raw = os.environ.get("UA_VAULT_WRITE_DIRS", DEFAULT_VAULT_WRITE_DIRS)
    return [d.strip().strip("/") for d in raw.split(":") if d.strip()]


def vault_require_revision() -> bool:
    """``UA_VAULT_REQUIRE_REVISION`` (task #2111, default off).

    On, ``vault_write`` refuses to overwrite an existing note unless the caller
    passes the ``expected_revision`` it read. Creates and appends stay allowed.
    """
    return _env_on("UA_VAULT_REQUIRE_REVISION")


# --- Vault sync (task #1288, extends task #816/PR #421) -------------------
#
# Continuous hub -> desktop vault sync: the explicitly configured remote vault
# is authoritative for the trees below; the desktop pulls on a timer via SSH.
# See vault_sync.py and docs/vault-sync-operations.md.

VAULT_SYNC_DEFAULT_INTERVAL_MINUTES = 10


def vault_sync_local_root(env: Mapping[str, str] | None = None) -> Path:
    """Local vault root the sync pulls into. Defaults to ``vault_root()``."""
    values = os.environ if env is None else env
    raw = values.get("UA_VAULT_SYNC_LOCAL_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser()
    raw = values.get("UA_VAULT_PATH", "").strip()
    if not raw:
        raise ValueError("UA_VAULT_PATH is disabled; set UA_VAULT_SYNC_LOCAL_ROOT to sync")
    return Path(raw).expanduser()


def vault_sync_remote_host(env: Mapping[str, str] | None = None) -> str:
    """Explicit SSH host alias; empty disables sync targeting."""
    return (os.environ if env is None else env).get("UA_VAULT_SYNC_REMOTE_HOST", "").strip()


def _node_names(raw: str) -> tuple[str, ...]:
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def fleet_nodes(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """ua-node names the operator expects live (``UA_FLEET_NODES``, comma-separated).

    Empty means no fleet is declared, and doctor's ``fleet_nodes`` row stays
    DORMANT rather than guessing which hosts matter.
    """
    return _node_names((os.environ if env is None else env).get("UA_FLEET_NODES", ""))


def fleet_optional_nodes(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """ua-node names the operator declared optional (``UA_FLEET_OPTIONAL_NODES``).

    An optional node that is not reporting reads as absent by decision, never
    as healthy and never as a fault. A name also listed as required is required.
    """
    required = fleet_nodes(env)
    raw = (os.environ if env is None else env).get("UA_FLEET_OPTIONAL_NODES", "")
    return tuple(name for name in _node_names(raw) if name not in required)


def vault_sync_remote_root(env: Mapping[str, str] | None = None) -> str:
    """Explicit remote vault path, relative to the SSH login's home directory."""
    return (os.environ if env is None else env).get("UA_VAULT_SYNC_REMOTE_ROOT", "").strip()


def vault_sync_interval_minutes(env: Mapping[str, str] | None = None) -> int:
    """Scheduling interval in minutes — informational for the timer unit and
    the staleness budget doctor's ``vault_sync`` check uses (3x this value)."""
    raw = (os.environ if env is None else env).get("UA_VAULT_SYNC_INTERVAL_MINUTES")
    if raw is None:
        return VAULT_SYNC_DEFAULT_INTERVAL_MINUTES
    try:
        val = int(raw)
    except ValueError:
        return VAULT_SYNC_DEFAULT_INTERVAL_MINUTES
    return val if val > 0 else VAULT_SYNC_DEFAULT_INTERVAL_MINUTES


# Vault sync (vault_sync.py): hub-authoritative trees, relative to the vault root. Kept as a tuple (not a
# set) so iteration order is deterministic in logs/tests.
DEFAULT_SYNCED_TREES: tuple[str, ...] = ("Wiki", "Logs", "00 - Inbox")

# Retired paths (task #1256): a tree name or path prefix, relative to the vault
# root, that must never be resurrected by a pull even if it reappears on the
# hub. Extending UA_VAULT_SYNC_RETIRED_PATHS adds to this list; it cannot
# remove from it (see config.vault_sync_retired_paths docstring) — a footgun
# already burned once with UA_AUDIT_ROOTS' replace-not-append semantics, and
# this list exists specifically to make a mistake like #1256 hard to repeat.
DEFAULT_RETIRED_PATHS: tuple[str, ...] = ("Logs/Laptop",)


def vault_sync_synced_trees() -> tuple[str, ...]:
    """Hub-authoritative trees to pull, colon-separated (same delimiter as
    ``UA_VAULT_WRITE_DIRS``). Replace-not-append: set the full list you want."""
    raw = os.environ.get("UA_VAULT_SYNC_TREES")
    if raw is None:
        return DEFAULT_SYNCED_TREES
    trees = tuple(dict.fromkeys(t.strip() for t in raw.split(":") if t.strip()))
    return (
        tuple(
            tree
            for tree in trees
            if not any(tree.startswith(other + "/") for other in trees if other != tree)
        )
        or DEFAULT_SYNCED_TREES
    )


def vault_sync_retired_paths() -> tuple[str, ...]:
    """Paths that must never be resurrected by a pull (task #1256).

    UNION-with-default, deliberately unlike ``UA_AUDIT_ROOTS``'
    replace-not-append semantics (a footgun documented at length in this
    repo's cockpit CLAUDE.md gotchas) — a retired-paths list exists precisely
    so a config change elsewhere cannot silently drop the protection for a
    path someone already had to retire once. ``UA_VAULT_SYNC_RETIRED_PATHS``
    adds entries; it cannot remove ``Logs/Laptop``.
    """
    raw = os.environ.get("UA_VAULT_SYNC_RETIRED_PATHS", "")
    extra = tuple(p.strip().strip("/") for p in raw.split(":") if p.strip())
    seen: list[str] = []
    for p in (*DEFAULT_RETIRED_PATHS, *extra):
        if p not in seen:
            seen.append(p)
    return tuple(seen)


def vault_sync_ignored_globs() -> tuple[str, ...]:
    """Machine-local files never synced in either direction (fnmatch globs).

    Defaults cover what a real hub/desktop dry run turned up (2026-09-23):
    this host's own ``Logs/state_*.<hostname>.*`` snapshot mirrors (written
    here, so the hub's copy is always older), Obsidian ``.space`` caches, and
    ``*.tmp*`` writer leftovers. ``UA_VAULT_SYNC_IGNORE`` (``:``-separated)
    adds to the defaults.
    """
    import socket

    host = socket.gethostname().split(".")[0]
    source_host = (
        state_mirror_name("state_memory", ".md").removeprefix("state_memory.").removesuffix(".md")
    )
    defaults = tuple(
        dict.fromkeys(
            (
                f"Logs/state_*.{host}.*",
                f"Logs/state_*.{source_host}.*",
                "*/.space/*",
                "*.tmp*",
            )
        )
    )
    raw = os.environ.get("UA_VAULT_SYNC_IGNORE", "")
    extra = tuple(g.strip() for g in raw.split(":") if g.strip())
    return tuple(dict.fromkeys((*defaults, *extra)))


def vault_sync_push_conflicts_enabled() -> bool:
    """When true, a desktop-side conflict is also copied into the hub's
    ``00 - Inbox`` (best-effort) for triage there, in addition to the local
    ``<name>.conflict-<timestamp>.md`` copy. Off by default: the default mode
    never writes to the hub at all, only reads from it."""
    return os.environ.get("UA_VAULT_SYNC_PUSH_CONFLICTS", "0") == "1"


def vault_sync_ssh_multiplex_enabled() -> bool:
    """Share one SSH connection (ControlMaster) across a sync run (task #1492).

    On by default: a run makes several SSH calls per pulled file, and one fresh
    connection per call made the Mac hub refuse exec for 20+ minutes after a
    burst. ``UA_VAULT_SYNC_SSH_MULTIPLEX=0`` restores one connection per call."""
    return os.environ.get("UA_VAULT_SYNC_SSH_MULTIPLEX", "1") != "0"


def vault_sync_ssh_control_dir() -> Path:
    """Private directory for the vault-sync SSH control socket.

    ``$XDG_RUNTIME_DIR`` when set (tmpfs, per user, cleared at logout), else
    ``UA_HOME``. Kept short: a unix socket path must fit the 104-byte macOS
    ``sun_path`` limit after ssh expands ``%C`` to a 40-character hash."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("UA_HOME", "~/.unified_agent"))
    return base.expanduser() / "ua-ssh"


def vault_sync_dry_run_default() -> bool:
    """Env-level dry-run switch, checked in addition to the CLI ``--dry-run`` flag."""
    return os.environ.get("UA_VAULT_SYNC_DRY_RUN", "0") == "1"


def vault_sync_state_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("UA_HOME", "~/.unified_agent")).expanduser() / "vault-sync-state.json"


def obsidian_api_url() -> str:
    """Local REST API base URL, empty when the runtime bridge is not configured."""
    return os.environ.get("UA_OBSIDIAN_API_URL", "").strip()


def obsidian_api_key() -> str:
    """Local REST API bearer key, empty when the runtime bridge is not configured."""
    return os.environ.get("UA_OBSIDIAN_API_KEY", "").strip()


def executor_max_attempts() -> int:
    """Max execution attempt ceiling for autonomous delegations (default 5)."""
    raw = os.environ.get("UA_EXECUTOR_MAX_ATTEMPTS")
    if not raw:
        return 5
    try:
        val = int(raw)
        return val if val > 0 else 5
    except ValueError:
        return 5


def executor_backoff_base_s() -> float:
    """Base backoff window in seconds for retries (default 10.0s)."""
    raw = os.environ.get("UA_EXECUTOR_BACKOFF_BASE_S")
    if not raw:
        return 10.0
    try:
        val = float(raw)
        return val if val > 0 else 10.0
    except ValueError:
        return 10.0


DEFAULT_EXECUTOR_AGENT = ""


def executor_agent() -> str:
    """Identity the Active Executor claims as. Empty when ``UA_EXECUTOR_AGENT`` is unset.

    There is no compiled fleet default — a third-party install must set the
    variable (the sample deploy units do). ``executor.daemon.main`` refuses to
    start without one.
    """
    raw = os.environ.get("UA_EXECUTOR_AGENT", DEFAULT_EXECUTOR_AGENT)
    return (raw or "").strip()


def _node_hosted_or_public() -> bool:
    # Same hosted indicators as execution_locality, available even while the
    # recovered boundary module is integrated independently of this change.
    return (
        bool((os.environ.get("RAILWAY_ENVIRONMENT_ID") or "").strip())
        or ((os.environ.get("UA_HOSTED") or "").strip() == "1")
        or deployment_mode() == DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC
    )


def node_allow_bearer() -> bool:
    """Whether a static bearer may act as a ua-node (task #1327). Off by default.

    A node runs agent CLIs on a device, so the device plane accepts only the
    device-flow credential `unified-agent login` issues (``auth_method:
    device_code``). ``UA_NODE_ALLOW_BEARER=1`` admits static bearers too, for a
    private hub without the OAuth server or for a lab network. The public
    hosted mode always refuses static bearer device-plane access.
    """
    return (
        not _node_hosted_or_public() and os.environ.get("UA_NODE_ALLOW_BEARER", "0").strip() == "1"
    )


def node_require_acceptance() -> bool:
    """Whether a ua-node must pass device acceptance before real work (task #2112).

    On by default in authenticated_public mode; off by default locally.
    With ``UA_NODE_REQUIRE_ACCEPTANCE=1`` the hub refuses to
    queue or route anything but the acceptance job itself to a node whose
    latest acceptance run is not ``accepted`` (``node_acceptance``).
    """
    default = "1" if _node_hosted_or_public() else "0"
    return os.environ.get("UA_NODE_REQUIRE_ACCEPTANCE", default).strip() == "1"


def acp_session_mcp_enabled() -> bool:
    """Whether session-scoped hub MCP credentials are on (task #1744). Off by default.

    ``UA_ACP_SESSION_MCP=on`` turns it on; any other value is off. Read on
    every request, so switching it off disables every outstanding credential
    at once (a kill switch, not a graceful rollback).
    """
    return os.environ.get("UA_ACP_SESSION_MCP", "off").strip().lower() == "on"


def acp_session_mcp_nodes() -> frozenset[str] | None:
    """Node allowlist for minting session credentials, or None for every node.

    Read at mint only, so removing a node leaves its running sessions alone and
    its new sessions fall back. Unset means every node; set (even to an empty
    value) means only the listed nodes, so emptying the list mints for none.
    """
    raw = os.environ.get("UA_ACP_SESSION_MCP_NODES")
    if raw is None:
        return None
    return frozenset(part.strip() for part in raw.replace("\n", ",").split(",") if part.strip())


def acp_session_device_check() -> str:
    """``strict`` (default) or ``best_effort`` for the session credential's peer check.

    ``best_effort`` is for a hub behind a local reverse proxy, which sees every
    client as loopback: an unresolvable peer is then admitted and the decision
    records ``device_check: "unavailable"``. A resolvable peer must still match.
    Any other value is ``strict``.
    """
    raw = os.environ.get("UA_ACP_SESSION_DEVICE_CHECK", "strict").strip().lower()
    return "best_effort" if raw == "best_effort" else "strict"


def node_session_mcp_enabled() -> bool:
    """Device side: whether this ua-node asks for session credentials (task #1744).

    On by default; ``UA_NODE_SESSION_MCP=0`` opts the node out. An opted-out
    node neither advertises the feature nor calls the mint route.
    """
    return os.environ.get("UA_NODE_SESSION_MCP", "1").strip() != "0"


def node_wake_enabled() -> bool:
    """Device side: whether this ua-node runs the idle-session wake loop (task #1873).

    Off by default; ``UA_NODE_WAKE=1`` turns it on. Each session still requests
    wakes (launched with ``UA_WAKE=1``) and needs the operator's grant (#2050);
    this only lets the node type wakes.
    """
    return os.environ.get("UA_NODE_WAKE", "0").strip() == "1"


def executor_backend() -> str:
    """Which execution backend the Active Executor runs.

    Empty when unset: the daemon then selects the native headless CLI for
    ``UA_EXECUTOR_AGENT`` (Claude → ``claude -p``, Codex → ``codex exec``, …).
    ``pi`` is an explicit alias, not the default — wearing Pi as another
    agent's label is how Desktop_agent3 used to run the wrong persona.

    Returned unvalidated-but-normalised on purpose: the executor's backend
    registry decides what is a legal name and raises on anything else, so a
    typo'd backend stops the daemon instead of silently falling back to Pi.
    """
    raw = os.environ.get("UA_EXECUTOR_BACKEND")
    if raw is None or not str(raw).strip():
        return ""
    return str(raw).strip().lower()


def executor_env_passthrough() -> tuple[tuple[str, str], ...]:
    """Extra parent variables an executor backend child may inherit (task #1910).

    ``UA_EXECUTOR_ENV_PASSTHROUGH`` is a comma-separated list. ``NAME`` applies
    to every backend; ``backend:NAME`` (e.g. ``codex:OPENAI_API_KEY``) to that
    backend only. Returned as ``(backend, NAME)`` pairs with ``"*"`` for an
    unscoped entry. Policy (secret names must be scoped, hub secrets are never
    passed) is applied by ``executor.child_env``, not here.
    """
    raw = os.environ.get("UA_EXECUTOR_ENV_PASSTHROUGH") or ""
    out: list[tuple[str, str]] = []
    for part in raw.replace("\n", ",").split(","):
        entry = part.strip()
        if not entry:
            continue
        backend, sep, name = entry.rpartition(":")
        backend = backend.strip().lower() if sep else "*"
        name = name.strip()
        if name and backend:
            out.append((backend, name))
    return tuple(out)


def executor_backoff_factor() -> float:
    """Exponential backoff multiplier factor (default 2.0)."""
    raw = os.environ.get("UA_EXECUTOR_BACKOFF_FACTOR")
    if not raw:
        return 2.0
    try:
        val = float(raw)
        return val if val >= 1.0 else 2.0
    except ValueError:
        return 2.0


def _executor_seconds(name: str, default: float, *, allow_zero: bool) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if not math.isfinite(val) or val < 0 or (val == 0 and not allow_zero):
        return default
    return val


DEFAULT_EXECUTOR_BACKOFF_CAP_S = 3600.0
DEFAULT_EXECUTOR_RECLAIM_INTERVAL_S = 300.0
DEFAULT_EXECUTOR_STALL_TIMEOUT_S = 1800.0


def executor_backoff_cap_s() -> float:
    """Ceiling on one retry backoff window (default 3600s, task #1916).

    The window is ``min(base * factor^(attempts-1), cap)``.
    """
    return _executor_seconds(
        "UA_EXECUTOR_BACKOFF_CAP_S", DEFAULT_EXECUTOR_BACKOFF_CAP_S, allow_zero=False
    )


def executor_reclaim_interval_s() -> float:
    """Seconds between the daemon's expired-lock reclaim sweeps (default 300).

    Task #1916: the sweep used to run only at startup. ``0`` keeps that
    startup-only behaviour.
    """
    return _executor_seconds(
        "UA_EXECUTOR_RECLAIM_INTERVAL_S", DEFAULT_EXECUTOR_RECLAIM_INTERVAL_S, allow_zero=True
    )


def executor_stall_timeout_s() -> float:
    """Seconds a live child may go without output before it counts as stalled.

    Default 1800 (task #1916). ``0`` disables stall detection. It only ever
    fires before the run's own wall-clock timeout, so with the default
    ``UA_EXECUTOR_TIMEOUT_S`` of 300 it matters only on hosts that raised it.
    """
    return _executor_seconds(
        "UA_EXECUTOR_STALL_TIMEOUT_S", DEFAULT_EXECUTOR_STALL_TIMEOUT_S, allow_zero=True
    )


DEFAULT_EXECUTOR_TIMEOUT_S = 300


def executor_timeout_s() -> int:
    """Child-run wall-clock timeout for unbounded executor runs (default 300s).

    Cycle-bound runs still take ``min(remaining_budget, this value)``. Invalid,
    zero, or negative values fall back to 300 with a warning — never a
    zero-timeout. Opt-in per host via ``UA_EXECUTOR_TIMEOUT_S``.
    """
    raw = os.environ.get("UA_EXECUTOR_TIMEOUT_S")
    if not raw:
        return DEFAULT_EXECUTOR_TIMEOUT_S
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "UA_EXECUTOR_TIMEOUT_S=%r is not an integer; using default %s",
            raw,
            DEFAULT_EXECUTOR_TIMEOUT_S,
        )
        return DEFAULT_EXECUTOR_TIMEOUT_S
    if val <= 0:
        logger.warning(
            "UA_EXECUTOR_TIMEOUT_S=%r is not positive; using default %s",
            raw,
            DEFAULT_EXECUTOR_TIMEOUT_S,
        )
        return DEFAULT_EXECUTOR_TIMEOUT_S
    return val


DEFAULT_REVIEW_PYTEST_TIMEOUT_S = 1800


def review_pytest_timeout_s() -> int:
    """Wall-clock ceiling for the merge/review pipeline's pytest run (default 1800s).

    Task #1254: hardcoded at 600s, this tripped false "pytest timed out"
    verdicts once the suite (4,769 -> 4,967 tests in one day) started running
    447-488s on the Mac — below the old ceiling with no margin. 1800s gives
    headroom well past observed runtime. Invalid, zero, or negative values
    fall back to the default with a warning — never a zero-timeout. Override
    via ``UA_REVIEW_PYTEST_TIMEOUT_S``.
    """
    raw = os.environ.get("UA_REVIEW_PYTEST_TIMEOUT_S")
    if not raw:
        return DEFAULT_REVIEW_PYTEST_TIMEOUT_S
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "UA_REVIEW_PYTEST_TIMEOUT_S=%r is not an integer; using default %s",
            raw,
            DEFAULT_REVIEW_PYTEST_TIMEOUT_S,
        )
        return DEFAULT_REVIEW_PYTEST_TIMEOUT_S
    if val <= 0:
        logger.warning(
            "UA_REVIEW_PYTEST_TIMEOUT_S=%r is not positive; using default %s",
            raw,
            DEFAULT_REVIEW_PYTEST_TIMEOUT_S,
        )
        return DEFAULT_REVIEW_PYTEST_TIMEOUT_S
    return val


DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S = 600


def review_bun_test_timeout_s() -> int:
    """Wall-clock ceiling for the merge/review pipeline's `bun test` run (default 600s).

    Task #1295 follow-up to #1254: `_run_bun_test` hardcoded 600s with no way
    to raise it, unlike `review_pytest_timeout_s()`. A sibling knob rather
    than reusing ``UA_REVIEW_PYTEST_TIMEOUT_S``: pytest and bun-test suites in
    this workspace have unrelated sizes and runtimes (the pytest ceiling was
    raised to 1800s to clear a ~450-490s suite; nothing says a bun suite needs
    the same headroom), and one knob would force them to move together —
    tightening one for its own suite would silently change the other's
    ceiling too. Invalid, zero, or negative values fall back to the default
    with a warning — never a zero-timeout. Override via
    ``UA_REVIEW_BUN_TEST_TIMEOUT_S``.
    """
    raw = os.environ.get("UA_REVIEW_BUN_TEST_TIMEOUT_S")
    if not raw:
        return DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "UA_REVIEW_BUN_TEST_TIMEOUT_S=%r is not an integer; using default %s",
            raw,
            DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S,
        )
        return DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S
    if val <= 0:
        logger.warning(
            "UA_REVIEW_BUN_TEST_TIMEOUT_S=%r is not positive; using default %s",
            raw,
            DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S,
        )
        return DEFAULT_REVIEW_BUN_TEST_TIMEOUT_S
    return val


DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES = 2


def review_timeout_max_retries() -> int:
    """Max pytest-timeout attempts the review pipeline retries at one branch-head
    sha before holding as ``unchanged`` (default 2).

    Task #1254 follow-up: a ``timeout`` verdict is deliberately excluded from
    the at-sha idempotence check so a transient timeout gets retried on the
    next scan — but unbounded, a suite that genuinely never finishes would
    re-run a (now up to ``UA_REVIEW_PYTEST_TIMEOUT_S``-long) pytest on every
    ``*/15`` cron tick, bloat the task notes with one line per scan, and send
    a phone notification every time. This caps it: once
    ``UA_REVIEW_TIMEOUT_MAX_RETRIES`` timeout notes already exist for the
    current sha, the gates are not re-run until a new commit lands. Invalid,
    zero, or negative values fall back to the default with a warning.
    """
    raw = os.environ.get("UA_REVIEW_TIMEOUT_MAX_RETRIES")
    if not raw:
        return DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "UA_REVIEW_TIMEOUT_MAX_RETRIES=%r is not an integer; using default %s",
            raw,
            DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES,
        )
        return DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES
    if val <= 0:
        logger.warning(
            "UA_REVIEW_TIMEOUT_MAX_RETRIES=%r is not positive; using default %s",
            raw,
            DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES,
        )
        return DEFAULT_REVIEW_TIMEOUT_MAX_RETRIES
    return val


def review_trust_ci() -> bool:
    """When True, exact-head green CI pytest shards substitute for local pytest.

    Task #1338: the ``*/15`` review cron was re-running the full suite (up to
    ``UA_REVIEW_PYTEST_TIMEOUT_S``) on the Mac hub that also serves MCP. CI
    (GitHub Actions, or a dispatched Depot run) already runs the sharded suite
    at the PR head. Default on
    (``UA_REVIEW_TRUST_CI=1``); set ``0``/``false``/``no``/``off`` to force
    local pytest always.
    """
    return not env_flag_off("UA_REVIEW_TRUST_CI")


# Check names for the CI pytest shards (``.github/workflows/ci.yml`` job
# ``test``, matrix shard 0..3). The reviewer normalizes rollup names with
# ``pr_models.rollup_check_name``, so GitHub Actions (``test (shard=0)`` in
# workflow ``CI``) and Depot CI (``CI / test (shard=0)``) both match (#1787).
DEFAULT_REVIEW_CI_REQUIRED_CHECKS: tuple[str, ...] = (
    "CI / test (shard=0)",
    "CI / test (shard=1)",
    "CI / test (shard=2)",
    "CI / test (shard=3)",
)


def review_ci_required_checks() -> list[str]:
    """Check names that must be SUCCESS at the exact PR head to skip local pytest.

    Override with colon-separated ``UA_REVIEW_CI_REQUIRED_CHECKS``. Empty or
    whitespace-only override falls back to the shard defaults. SKIPPED
    does not count — path-gated PRs without a Python run still fall back to
    local pytest.
    """
    raw = os.environ.get("UA_REVIEW_CI_REQUIRED_CHECKS")
    if not raw:
        return list(DEFAULT_REVIEW_CI_REQUIRED_CHECKS)
    names = [part.strip() for part in raw.split(":") if part.strip()]
    return names or list(DEFAULT_REVIEW_CI_REQUIRED_CHECKS)


DEFAULT_REVIEW_TARGETED_MAX_FILES = 40


def review_local_pytest_scope() -> str:
    """What the review pipeline runs locally when exact-head CI cannot stand in.

    Task #1787: CI no longer runs on every PR head (batch verification), so
    "no CI shards" is the normal case. ``targeted`` (default) runs only the
    test files the diff touches or names (``reviewer._targeted_test_paths``)
    and holds the task as ``awaiting-ci`` when there are none, so the Mac hub
    never runs the full suite (the CPU trouble behind #1338 and #1786).
    ``full`` restores the old whole-suite run for hosts that can afford it.
    Set with ``UA_REVIEW_LOCAL_PYTEST``; anything else means ``targeted``.
    """
    raw = os.environ.get("UA_REVIEW_LOCAL_PYTEST", "targeted").strip().lower()
    return "full" if raw == "full" else "targeted"


def review_targeted_max_files() -> int:
    """Most test files a targeted review run may select (``UA_REVIEW_TARGETED_MAX_FILES``).

    A diff that maps to more test files than this is close to a full-suite
    run, so the reviewer holds it as ``awaiting-ci`` instead. Default 40.
    """
    raw = os.environ.get("UA_REVIEW_TARGETED_MAX_FILES")
    if not raw:
        return DEFAULT_REVIEW_TARGETED_MAX_FILES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "UA_REVIEW_TARGETED_MAX_FILES=%r is not an integer; using default %s",
            raw,
            DEFAULT_REVIEW_TARGETED_MAX_FILES,
        )
        return DEFAULT_REVIEW_TARGETED_MAX_FILES
    return value if value > 0 else DEFAULT_REVIEW_TARGETED_MAX_FILES


def executor_allow_untrusted() -> bool:
    """Whether the executor daemon is permitted to execute tasks marked untrusted=1.

    Defaults to False (Executor Trust Ladder Stage 2: trusted tasks only).
    Override via UA_EXECUTOR_ALLOW_UNTRUSTED=1 for Stage 5 quarantine execution.
    """
    return os.environ.get("UA_EXECUTOR_ALLOW_UNTRUSTED", "0") == "1"


# --- per-agent / per-run model selection (task #1153) ----------------------------
# Public model ids only — never secrets. Invalid values warn and fall through so
# unset envs + no ``[model:]`` line keep today's backend behaviour byte-for-byte.

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-\[\]=,:+]{0,127}$")
_MODEL_OVERRIDE_RE = re.compile(r"\[model:\s*([^\]]+)\]", re.IGNORECASE)

# Family token (from Desktop_/Laptop_/server_ labels, or bare pi*) → UA_MODEL_* suffix.
_AGENT_MODEL_FAMILY_SUFFIX: dict[str, str] = {
    "cursor": "CURSOR",
    "codex": "CODEX",
    "agy": "AGY",
    "claude": "CLAUDE",
    "pi": "PI",
    "devin": "DEVIN",
}

_MODEL_ENV_KEYS = (
    "UA_MODEL_CURSOR",
    "UA_MODEL_CODEX",
    "UA_MODEL_AGY",
    "UA_MODEL_CLAUDE",
    "UA_MODEL_PI",
    "UA_MODEL_DEVIN",
    "UA_MODEL_DEFAULT",
)

# Fleet seeds set by the operator (2026-09-19). Shown in docs + Control Center selector
# options. NOT applied at run time when the matching env is unset — that keeps
# unset-env behaviour byte-for-byte with today's backends.
CANONICAL_MODEL_DEFAULTS: dict[str, str] = {
    "UA_MODEL_CURSOR": "auto",
    "UA_MODEL_CODEX": "gpt-6-luna",
    "UA_MODEL_AGY": "gemini-3.8-flash-high",
    "UA_MODEL_CLAUDE": "sonnet-5",
}

# Named alternatives always offered in the Dispatch Studio selector.
CANONICAL_MODEL_ALTERNATES: tuple[str, ...] = ("cursor-grok-4.6-high-fast",)


def sanitize_model_id(raw: str | None, *, source: str = "model") -> str | None:
    """Return a cleaned model id, or None after warning on empty/invalid input."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if not _MODEL_ID_RE.fullmatch(value):
        logger.warning("%s=%r is not a valid model id; ignoring", source, raw)
        return None
    return value


def parse_model_override(text: str | None) -> str | None:
    """Extract the last ``[model: <id>]`` marker from a task/delegation spec.

    Invalid ids log a warning and are ignored (fall through to configured default).
    """
    if not text:
        return None
    matches = list(_MODEL_OVERRIDE_RE.finditer(text))
    if not matches:
        return None
    return sanitize_model_id(matches[-1].group(1), source="[model:]")


# Label segment per developer family. Identity in the product; the public
# export's scrub rewrites these values to neutral placeholders so a shipped
# tree does not name the fleet's agents. Anything that needs to know an
# agent's family from its label resolves it through agent_family() below,
# never by parsing the raw label text.
LABEL_BY_FAMILY: dict[str, str] = {
    "Claude": "Agent1",
    "Codex": "Agent2",
    "agy": "agent3",
    "Cursor": "Agent4",
    "Hermes": "Agent5",
    "Copilot": "Agent6",
    "Devin": "Agent7",
}
SEGMENT_TO_FAMILY: dict[str, str] = {seg: fam for fam, seg in LABEL_BY_FAMILY.items()}
SEGMENT_TO_FAMILY_LOWER: dict[str, str] = {seg.lower(): fam for fam, seg in LABEL_BY_FAMILY.items()}


def agent_family(agent_label: str | None) -> str | None:
    """Resolve the developer family from a roster label, or None.

    Parses ``<host>_<segment>`` through the label-segment map, so a neutralised
    export label (``Desktop_Agent1``) still resolves to ``Claude``. Case-exact
    segments are tried first, then the lowercase form used by ``server_*``.
    """
    label = (agent_label or "").strip()
    if "_" not in label:
        return None
    _host, segment = label.split("_", 1)
    return SEGMENT_TO_FAMILY.get(segment) or SEGMENT_TO_FAMILY_LOWER.get(segment)


def _agent_model_family(agent_label: str | None) -> str | None:
    """Map an agent label to a model-family token (cursor/codex/agy/claude/pi)."""
    label = (agent_label or "").strip()
    if not label:
        return None
    lower = label.lower()
    if lower in {"pi", "pi_worker"} or lower.startswith("pi_"):
        return "pi"
    family = agent_family(label)
    if family is not None:
        token = family.lower()
        if token in _AGENT_MODEL_FAMILY_SUFFIX:
            return token
    if "_" in lower:
        suffix = lower.rsplit("_", 1)[1]
        if suffix in _AGENT_MODEL_FAMILY_SUFFIX:
            return suffix
    return None


def agent_model_env_key(agent_label: str | None) -> str | None:
    """Env var name for this agent's family default (e.g. ``UA_MODEL_CURSOR``)."""
    family = _agent_model_family(agent_label)
    if family is None:
        return None
    return f"UA_MODEL_{_AGENT_MODEL_FAMILY_SUFFIX[family]}"


def agent_model_default(agent_label: str | None) -> str | None:
    """Configured default model for ``agent_label``.

    Reads ``UA_MODEL_<SUFFIX>`` (CURSOR/CODEX/AGY/CLAUDE/PI), then
    ``UA_MODEL_DEFAULT``. Returns None when nothing is configured so each
    backend keeps its own legacy default (``UA_CODEX_MODEL``, unset agy
    model, etc.). Canonical fleet seeds are *not* applied here — only via
    env — so unset stays byte-for-byte with prior behaviour.
    """
    key = agent_model_env_key(agent_label)
    if key:
        valued = sanitize_model_id(os.environ.get(key), source=key)
        if valued is not None:
            return valued
    return sanitize_model_id(os.environ.get("UA_MODEL_DEFAULT"), source="UA_MODEL_DEFAULT")


def configured_model_defaults() -> dict[str, str | None]:
    """Snapshot of env-configured model defaults (None when unset)."""
    out: dict[str, str | None] = {}
    for key in _MODEL_ENV_KEYS:
        out[key] = sanitize_model_id(os.environ.get(key), source=key)
    return out


def model_defaults_for_selector() -> dict[str, str | None]:
    """Env defaults with canonical fleet seeds filling unset family keys.

    Used by Control Center so the selector lands pre-populated with the operator's
    fleet ids. Run resolution still uses :func:`agent_model_default` (env only).
    """
    configured = configured_model_defaults()
    out: dict[str, str | None] = dict(configured)
    for key, seed in CANONICAL_MODEL_DEFAULTS.items():
        if out.get(key) is None:
            out[key] = seed
    return out


def model_selector_options() -> list[str]:
    """Sorted unique model ids for the Dispatch Studio dropdown."""
    opts: set[str] = set()
    for value in model_defaults_for_selector().values():
        if value:
            opts.add(value)
    opts.update(CANONICAL_MODEL_ALTERNATES)
    return sorted(opts)


def resolve_run_model(
    agent_label: str | None,
    *,
    override: str | None = None,
    backend_fallback: str | None = None,
) -> str | None:
    """Resolve the model for one run: override → agent default → backend fallback."""
    if override is not None:
        cleaned = sanitize_model_id(override, source="model override")
        if cleaned is not None:
            return cleaned
    default = agent_model_default(agent_label)
    if default is not None:
        return default
    return sanitize_model_id(backend_fallback, source="backend model") if backend_fallback else None


def executor_circuit_breaker_threshold() -> int:
    """Number of consecutive failed executions before tripping daemon circuit breaker (default 3)."""
    raw = os.environ.get("UA_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD")
    if not raw:
        return 3
    try:
        val = int(raw)
        return val if val > 0 else 3
    except ValueError:
        return 3


def bug_worker_enabled() -> bool:
    """Emergency stop / enable for the SYSTEM bug worker (default off).

    Set UA_BUG_WORKER_ENABLED=1 to allow discover/claim/run-once. Deliberately
    opt-in so automation never runs unattended until an operator enables it.
    """
    return os.environ.get("UA_BUG_WORKER_ENABLED", "0") == "1"


def mobile_watch_enabled() -> bool:
    """Return whether the mobile projection/action routes are opted in (task #1311).

    These routes remain behind hub authentication, and the action route keeps
    its origin and CSRF checks. Default off: set ``UA_MOBILE_WATCH_ENABLED=1``
    after configuring an authenticated mobile client. The existing iPhone
    relay is distinct from Apple Watch; this flag does not imply a Watch
    consumer or push transport.
    """
    return os.environ.get("UA_MOBILE_WATCH_ENABLED", "0") == "1"


def mobile_watch_manifest_secret_status(env: Mapping[str, str]) -> dict[str, object]:
    """Expose the opt-in flag by name only to connector doctor/pane status."""
    flag = "UA_MOBILE_WATCH_ENABLED"
    enabled = env.get(flag, "0") == "1"
    return {
        "present": [flag] if enabled else [],
        "missing": [] if enabled else [flag],
        "satisfied": enabled,
    }


def bug_worker_max_attempts() -> int:
    """Per-ticket retry ceiling before escalation (default 3)."""
    raw = os.environ.get("UA_BUG_WORKER_MAX_ATTEMPTS")
    if not raw:
        return 3
    try:
        val = int(raw)
        return val if val > 0 else 3
    except ValueError:
        return 3


def dispatch_scheduler_enabled() -> bool:
    """Opt-in gate for approved-dispatch scheduler ticks (default off).

    Set ``UA_DISPATCH_SCHEDULER_ENABLED=1`` to allow cron/Kanban-driven
    ``scheduler_tick`` to reclaim leases and claim ready work. Code and CLI
    may exist while this stays ``0`` — production autonomy is gated by the operator.
    """
    return os.environ.get("UA_DISPATCH_SCHEDULER_ENABLED", "0") == "1"


def work_product_enabled() -> bool:
    """Opt-in gate for the work-product submit -> approval -> release wedge

    (task #1278, H2: the non-code pilot wedge — an agent-produced document,
    a named human approval, a released copy, and the evidence chain linking
    human -> device -> agent -> model -> document hash -> approval). Default
    off: set ``UA_WORK_PRODUCT_ENABLED=1`` to allow ``work_product_submit``
    and the ``unified-agent work-product`` CLI to run past the disabled
    refusal. Code, storage, and CLI exist unconditionally; only the write
    path is gated, following the same pattern as ``bug_worker_enabled`` and
    ``dispatch_scheduler_enabled``.
    """
    return os.environ.get("UA_WORK_PRODUCT_ENABLED", "0") == "1"


def mcp_gateway_enabled() -> bool:
    """Opt-in gate for the ``mcp_gateway`` tool (task #1336). Default off.

    With ``UA_MCP_GATEWAY=1`` the hub calls the downstream MCP servers named in
    ``UA_MCP_GATEWAY_SERVERS`` on a caller's behalf; off, the tool refuses as
    dormant and ``system_doctor`` reports the row DORMANT.
    """
    return os.environ.get("UA_MCP_GATEWAY", "0").strip() == "1"


def mcp_gateway_servers_raw() -> str | None:
    """``UA_MCP_GATEWAY_SERVERS``: the JSON downstream registry (task #1336).

    ``{name: {transport?, url | command, secret, allow?, ...}}``, parsed and
    validated by ``tools.mcp_gateway.load_registry``. ``secret`` names a managed
    secret bound to consumer ``mcp_gateway``; it is never a token.
    """
    return os.environ.get("UA_MCP_GATEWAY_SERVERS")


def mcp_gateway_discovery_ttl_s() -> float:
    """``UA_MCP_GATEWAY_DISCOVERY_TTL_S``: tool-list cache lifetime (task #1336).

    Default 300, clamped to 0-3600; 0 turns the cache off. Unparseable means default.
    """
    try:
        ttl = float(os.environ.get("UA_MCP_GATEWAY_DISCOVERY_TTL_S", "300"))
    except ValueError:
        return 300.0
    return 300.0 if ttl != ttl else max(0.0, min(ttl, 3600.0))


def work_product_release_dir() -> Path:
    """Local directory a work product's bytes are copied into on approval.

    Local path only for this slice (task #1278) — Drive/SharePoint
    destinations are an explicit out-of-scope follow-up. Defaults under
    ``UA_HOME`` so a default install has somewhere durable to release into
    without any extra configuration; a real pilot points
    ``UA_WORK_PRODUCT_RELEASE_DIR`` at its own managed location.
    """
    return Path(
        os.environ.get(
            "UA_WORK_PRODUCT_RELEASE_DIR", str(data_home() / "work_products" / "released")
        )
    ).expanduser()


def work_product_template_dir() -> Path:
    """Directory holding vertical-neutral template-pack YAML files.

    Defaults to the ``templates/work_product/`` directory shipped inside the
    package (resolved relative to this file, not the CWD, so it works from a
    worktree, an installed wheel, and the Docker image alike) — the pack
    format is data, not code, exactly like ``policy_file_path``'s YAML DSL.
    ``UA_WORK_PRODUCT_TEMPLATE_DIR`` lets an operator point at a separate,
    pilot-specific pack directory instead.
    """
    default_dir = Path(__file__).resolve().parent / "templates" / "work_product"
    return Path(os.environ.get("UA_WORK_PRODUCT_TEMPLATE_DIR", str(default_dir))).expanduser()


def dispatch_real_spawn() -> bool:
    """Opt-in gate for spawning a REAL agent CLI from scheduler ticks (default off).

    This is the boundary between "the dispatch machinery records a correct
    state transition" and "an agent process actually starts with no human at
    the keyboard". Until task #599 this was not configurable at all:
    ``RoutedLaunchBoundary`` hardcoded ``StubProcessSpawner``, and the only
    construction of ``CliProcessSpawner`` in the codebase was the Control
    Center operator-click path (``webui/api.py``). Scheduler ticks therefore
    claimed work, leased it, routed it, and launched nothing.

    Set ``UA_DISPATCH_REAL_SPAWN=1`` to let a tick exec the persona's headless
    CLI. Default stays ``0``: the code ships inert, exactly like
    ``dispatch_scheduler_enabled`` above, because turning it on is the first
    path in this project that starts an autonomous agent with no human in the
    loop at spawn time.

    Requires ``UA_DISPATCH_SCHEDULER_ENABLED=1`` to matter at all — a tick that
    never runs cannot spawn.
    """
    return os.environ.get("UA_DISPATCH_REAL_SPAWN", "0") == "1"


def dispatch_node_routing() -> bool:
    """Opt-in gate for routing scheduler launches to registered ua-nodes (default off).

    Independent of ``UA_DISPATCH_REAL_SPAWN``: node routing is its own explicit
    authorization to run dispatch work on a registered device, and does not
    require (or imply) local CLI spawn. With this off, scheduler ticks behave
    exactly as before — the launch boundary is never wrapped.
    """
    return os.environ.get("UA_DISPATCH_NODE_ROUTING", "0") == "1"


def dispatch_max_live_runs() -> int:
    """Ceiling on concurrently live dispatch runs (default 2, hard cap 16).

    Counts rows in ``dispatch_runs`` in state claimed/launched/running. Real
    spawn refuses once this many are live, so a scheduler bug, a stuck run, or
    a burst of approvals cannot fork an unbounded number of agent processes on
    the host. Unparseable or non-positive values fall back to the default
    rather than disabling the ceiling — a misconfigured cap must not mean "no
    cap".
    """
    raw = os.environ.get("UA_DISPATCH_MAX_LIVE_RUNS")
    if not raw:
        return 2
    try:
        val = int(raw)
    except ValueError:
        return 2
    if val <= 0:
        return 2
    return min(val, 16)


def dispatch_run_max_s() -> float:
    """Wall-clock ceiling for a single supervised dispatch run (default 3600s).

    Distinct from the claim lease TTL, and much larger. The lease answers "has
    this run gone quiet?"; this answers "has it run so long it must be a
    runaway?". Before task #602 only the lease existed, so a legitimately long
    agent session was reclaimed at 300s while still working and could be
    re-launched alongside itself.

    Set to 0 (or a negative value) to disable the ceiling entirely — an
    explicit operator choice, unlike the other dispatch knobs where a bad value
    falls back to the default. A run with no ceiling is still bounded by
    liveness: when the process dies, reclaim collects it.
    """
    raw = os.environ.get("UA_DISPATCH_RUN_MAX_S")
    if not raw:
        return 3600.0
    try:
        value = float(raw)
    except ValueError:
        return 3600.0
    # float("nan") parses and every comparison against NaN is False, so a hung
    # agent would hold its slot forever while the config claims a bound. Reject
    # NaN explicitly; infinity is an honest "no ceiling" (runtime_exceeded never
    # fires) and UA_DISPATCH_RUN_MAX_S=0 is the other explicit disable path.
    if math.isnan(value):
        return 3600.0
    return value


def dispatch_scheduler_max_attempts() -> int:
    """Retry ceiling before dead-letter/block (default 3)."""
    raw = os.environ.get("UA_DISPATCH_SCHEDULER_MAX_ATTEMPTS")
    if not raw:
        return 3
    try:
        val = int(raw)
        return val if val > 0 else 3
    except ValueError:
        return 3


def dispatch_scheduler_batch_limit() -> int:
    """Max ready claims per scheduler tick (default 10, capped at 50)."""
    raw = os.environ.get("UA_DISPATCH_SCHEDULER_BATCH_LIMIT")
    if not raw:
        return 10
    try:
        val = int(raw)
        if val <= 0:
            return 10
        return min(val, 50)
    except ValueError:
        return 10


def dispatch_scheduler_lease_ttl_s() -> float:
    """Claim lease TTL in seconds for scheduler-driven launches (default 300)."""
    from datetime import timedelta

    raw = os.environ.get("UA_DISPATCH_SCHEDULER_LEASE_TTL_S")
    if not raw:
        return 300.0
    try:
        val = float(raw)
        if not (val > 0 and val == val and val != float("inf")):
            return 300.0
        # Reject values timedelta cannot represent (e.g. 1e308, inf).
        timedelta(seconds=val)
        return val
    except (ValueError, OverflowError):
        return 300.0


def executor_circuit_breaker_cooldown_s() -> float:
    """Cooldown window in seconds when daemon circuit breaker trips (default 60.0)."""
    raw = os.environ.get("UA_EXECUTOR_CIRCUIT_BREAKER_COOLDOWN_S")
    if not raw:
        return 60.0
    try:
        val = float(raw)
        return val if val > 0 else 60.0
    except ValueError:
        return 60.0


def audit_roots() -> list[Path]:
    raw = os.environ.get("UA_AUDIT_ROOTS", "")
    return [Path(p).expanduser() for p in raw.split(":") if p]


# --- agent token lifecycle (task #1198) ---------------------------------------
# Bearer tokens have no built-in expiry (UA_AGENT_TOKENS is a static env-var
# list), so staleness can only be caught by comparing agent_tokens.issued_at
# against a policy age. 90 days matches this workspace's existing quarterly
# cadence for other credential hygiene (see rules/SHARED.md secrets hygiene)
# and is long enough that doctor does not cry wolf on a token rotated last
# sprint, short enough that a token disclosed in a log (as #1126 found nine of)
# is not live for the rest of the year before anyone notices it is old.
DEFAULT_TOKEN_MAX_AGE_DAYS = 90


def token_max_age_days() -> int:
    """Age (days) after which doctor warns an active agent_tokens row is stale.

    ``UA_TOKEN_MAX_AGE_DAYS``; misconfiguration keeps the default rather than
    disabling the warning (0/negative) or firing constantly (unparseable).
    """
    raw = os.environ.get("UA_TOKEN_MAX_AGE_DAYS")
    if raw is None:
        return DEFAULT_TOKEN_MAX_AGE_DAYS
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_TOKEN_MAX_AGE_DAYS
    return val if val > 0 else DEFAULT_TOKEN_MAX_AGE_DAYS


def search_provider() -> str:
    # 'auto' resolves at call time: a keyed provider when its API key is
    # present (see tools.web._resolve_provider), keyless ddg otherwise.
    return os.environ.get("UA_SEARCH_PROVIDER", "auto")


def allow_local_fetch() -> bool:
    return os.environ.get("UA_ALLOW_LOCAL_FETCH") == "1"


def approval_push_url() -> str:
    """Full ntfy-compatible topic URL for the waiting-approval push (task #1988).

    Empty (the default) leaves the notifier off. A secret: an ntfy topic name is
    a bearer capability, so never log, echo or record it; show the host only.
    """
    return os.environ.get("UA_APPROVAL_PUSH_URL", "").strip()


def approval_push_token() -> str:
    """Optional bearer token sent with the approval push. A secret, like the URL."""
    return os.environ.get("UA_APPROVAL_PUSH_TOKEN", "").strip()


def overwrite_author_pairs() -> set[frozenset[str]]:
    """Names cross-author pairs that may skip the memory_save tombstone on overwrite.

    Reads the comma-separated `UA_OVERWRITE_AUTHORS` env var, each entry of the
    form `author_a:author_b`. Pairs are unordered, so `A:B` and `B:A` both
    match. Malformed entries (no `:` separator, empty halves, self-pairs) are
    silently dropped — the allowlist is a feature, not a gate, and a typo
    should not break writes.

    The allowlist does NOT bypass the clobber guard (memory_save still refuses
    a cross-author replace without `overwrite=True`); it only suppresses the
    tombstone that would otherwise be appended to the replacing content.
    Same-author writes and index notes are never tombstones regardless.
    """
    raw = os.environ.get("UA_OVERWRITE_AUTHORS", "").strip()
    if not raw:
        return set()
    pairs: set[frozenset[str]] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        a, _, b = entry.partition(":")
        a, b = a.strip(), b.strip()
        if not a or not b or a == b:
            continue
        pairs.add(frozenset({a, b}))
    return pairs


def sandbox_enabled() -> bool:
    return os.environ.get("UA_SANDBOX_ENABLED") == "1"


def sandbox_engine() -> str:
    return os.environ.get("UA_SANDBOX_ENGINE", "auto")


def sandbox_image() -> str:
    return os.environ.get("UA_SANDBOX_IMAGE", "python:3.11-slim")


def sandbox_network() -> bool:
    return os.environ.get("UA_SANDBOX_NETWORK") == "1"


def state_sync_enabled() -> bool:
    return os.environ.get("UA_STATE_SYNC", "1") == "1"


def state_sync_dir() -> str:
    """The vault-relative directory where offline state sync snapshots land.

    Defaults to 'Logs'. Override via `UA_STATE_SYNC_DIR` to put a device's
    snapshots in their own directory. Cross-device filename collisions no
    longer depend on setting it — see `state_mirror_name`.
    """
    val = os.environ.get("UA_STATE_SYNC_DIR", "").strip()
    return val.strip("/") if val else "Logs"


def state_mirror_name(stem: str, ext: str) -> str:
    """Filename for a derived state mirror, scoped to the writing host.

    `state_memory` + `.md` -> `state_memory.DesktopHost.md`.

    These mirrors are derived: every machine regenerates them from its own
    memory.db. Sharing one filename across machines handed Obsidian Sync two
    writers and no merge strategy for markdown, so it deposited
    "(Conflicted copy <host> <time>)" duplicates of the largest generated files
    into the vault, polluting vault_search with obsolete snapshots (task #172).

    `UA_STATE_SYNC_DIR` already offered a way out by giving each device its own
    directory, and it failed for the only reason that matters: it is opt-in,
    and nobody set it. Scoping here is automatic — derived from the identity
    already stamped on every write — so a second machine cannot collide by
    default rather than by configuration.

    The host reaches a filename, so it is treated as untrusted input: anything
    outside [A-Za-z0-9._-] becomes a hyphen, which forecloses traversal and
    separators whatever the hostname or `UA_SOURCE_HOST` happens to contain.
    """
    host = re.sub(r"[^A-Za-z0-9._-]", "-", current_source_host() or "").strip(".-")
    return f"{stem}.{host or 'unknown-host'}{ext}"


# --- source_host: audit-trail identity of the machine originating a write ---
#
# The shared memory.db is mirrored across machines (desktop hub + laptop). Every
# memory note and task is stamped with the host that created it so the hub can
# tell its own writes apart from a peer's mirror (plan v3 §3). The value is
# resolved lazily so an env override (UA_SOURCE_HOST) applied after import — or
# in a test via monkeypatch — is always honored.
#
# Two questions, two functions (task #1633):
#
# * current_source_host() is the machine this process runs on: UA_SOURCE_HOST,
#   else the OS hostname. Devices, fleet_actions, the doctor, dispatch notes and
#   state-mirror filenames ask this, and a request must never move it.
# * write_source_host() is the host to stamp on a row written now. The hub's
#   auth middleware binds source_host_var to the verified caller's machine
#   (source_host.bind); unset, it is current_source_host().


class RequestHostBinding:
    """A caller host bound for one request's lifetime (task #1633).

    ``asyncio`` copies ContextVars into every task a request spawns, and
    resetting the request's var cannot reach those copies. They share this
    object, though, so ``end()`` at request cleanup makes a detached task's
    later writes stamp the hub instead of the departed caller.
    """

    __slots__ = ("host", "live")

    def __init__(self, host: str | None) -> None:
        self.host = host
        self.live = True

    def end(self) -> None:
        self.live = False


source_host_var: contextvars.ContextVar[str | RequestHostBinding | None] = contextvars.ContextVar(
    "ua_source_host", default=None
)


def _default_source_host() -> str:
    return os.environ.get("UA_SOURCE_HOST") or socket.gethostname()


def current_source_host() -> str:
    """The machine this process runs on: UA_SOURCE_HOST, else the OS hostname.

    Never the request's caller; see ``write_source_host`` for row stamps.
    """
    return _default_source_host()


def write_source_host() -> str:
    """The host to stamp on a row's ``source_host``: the request's caller, else this machine.

    The override is bound only by the hub's auth middleware, from the verified
    identity (``source_host.bind``), never from a request header. A binding
    whose request has ended (a detached task outliving it) stamps this machine.
    """
    override = source_host_var.get()
    if isinstance(override, RequestHostBinding):
        if override.live and override.host:
            return override.host
        return current_source_host()
    if override:
        return override
    return current_source_host()


# --- agent_identity: verified identity of the calling agent (HTTP bearer token) ---

agent_identity_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_agent_identity", default=None
)

auth_metadata_var: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "ua_auth_metadata", default=None
)

# Private sentinel. Production requests reach it through HubAuthASGIMiddleware,
# which calls bind_workos_request_auth(); that helper is the only caller of
# mark_workos_request_bound(). A caller that fabricates auth_metadata dict
# fields (including a forgeable idp_verified bool) does not get human
# attestation without also holding this binding (task #1313 / Devin on #753).
_WORKOS_BOUND = object()
_workos_bound_var: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "ua_workos_bound", default=None
)


def mark_workos_request_bound() -> contextvars.Token:
    """Stamp the private WorkOS-bound sentinel.

    Reached only from bind_workos_request_auth(), which HubAuthASGIMiddleware
    calls for a verified WorkOS session.
    """
    return _workos_bound_var.set(_WORKOS_BOUND)


def clear_workos_request_bound(token: contextvars.Token) -> None:
    _workos_bound_var.reset(token)


def bind_workos_request_auth(
    *,
    identity: str,
    subject: str,
    organization_id: str,
    session_id: str,
    expires_at: int | float,
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    """Bind a verified WorkOS session into request-local context.

    HubAuthASGIMiddleware calls this after the WorkOS leg resolves a known
    label. Tests use the same helper. Returns (identity, bound, metadata)
    tokens for reset.
    """
    id_tok = agent_identity_var.set(identity)
    bound_tok = mark_workos_request_bound()
    meta_tok = auth_metadata_var.set(
        {
            "auth_method": "workos",
            "scopes": [],
            "workos_user_id": subject,
            "workos_organization_id": organization_id,
            "workos_session_id": session_id,
            "exp": expires_at,
        }
    )
    return id_tok, bound_tok, meta_tok


def workos_request_is_bound() -> bool:
    """True only while the private WorkOS-bound sentinel is held."""
    return _workos_bound_var.get() is _WORKOS_BOUND


def reset_workos_request_auth(
    id_tok: contextvars.Token,
    bound_tok: contextvars.Token,
    meta_tok: contextvars.Token,
) -> None:
    agent_identity_var.reset(id_tok)
    clear_workos_request_bound(bound_tok)
    auth_metadata_var.reset(meta_tok)


# Streamable-HTTP assigns an opaque Mcp-Session-Id after initialize.  The hub
# middleware binds it for the duration of each request; profile activation uses
# that authenticated session boundary rather than a client-supplied agent label.
mcp_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_mcp_session_id", default=None
)

# Verified tailnet node identity of the calling device (task #1250, E3). The
# hub middleware resolves the peer's CGNAT address through local tailscaled
# (`tailscale whois`) and binds the result here for the request's duration, so
# producer-stamped events can persist which *device* acted — not just which
# identity label. None for loopback/non-tailnet/unresolvable peers.
peer_device_var: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "ua_peer_device", default=None
)


def verified_agent_identity() -> str | None:
    """The verified identity of the caller if authenticated via HTTP token."""
    return agent_identity_var.get()


def current_auth_metadata() -> dict[str, object] | None:
    """Request-local, non-secret metadata for the verified bearer credential."""
    return auth_metadata_var.get()


def current_mcp_session_id() -> str | None:
    """Opaque MCP session id bound by the HTTP middleware, if any."""
    return mcp_session_id_var.get()


def current_peer_device() -> dict[str, object] | None:
    """Verified tailnet node identity of the calling device, if resolved."""
    return peer_device_var.get()


# The human operator. A deployment names its own human through settings.toml
# ([operator] name/label/aliases) or UA_OPERATOR_NAME / UA_OPERATOR_LABEL /
# UA_OPERATOR_ALIASES; nothing about a particular person is compiled in.
#
#   label    the identity the human's own login resolves to (bearer token or
#            web UI session), e.g. ``Desktop_Alex``. It is deliberately left
#            unregistered as an agent label.
#   name     the bare name the same human signs approvals and delegations
#            with, e.g. ``Alex``.
#   aliases  any further claimed names for the same human.
DEFAULT_OPERATOR_NAME = "Operator"
DEFAULT_OPERATOR_LABEL = "Desktop_Operator"
# Returned when no valid operator label exists. Not a well-formed label, so no
# bearer, session or claimed author can ever equal it.
UNASSIGNED_OPERATOR_LABEL = "<unassigned-operator>"


def _names_worker_identity(name: str, *, include_roster: bool = True) -> bool:
    # Never traverse agent_labels()/agent_tokens()/registry here: they depend
    # on operator classification or load credential values from managed stores.
    key = name.strip().casefold()
    if key in {label.casefold() for label in _FLEET_AGENT_CATALOG}:
        return True
    if include_roster:
        roster = os.environ.get("UA_AGENT_LABELS", DEFAULT_AGENT_LABELS).split(":")
        return key in {label.strip().casefold() for label in roster if label.strip()}
    # Bare human names may appear in the addressing roster. The validated
    # primary login declares the human role; credential metadata alone cannot
    # distinguish a managed human token from a worker token.
    if key == operator_label().casefold():
        return False
    try:
        token_pairs = _parse_colon_pairs("UA_AGENT_TOKENS")
    except ValueError as exc:
        # Fail closed (task #1971): an unreadable token file cannot prove the
        # name is human, so refuse it like an unreadable credential store
        # (_managed_worker_label). The operator login label is decided above
        # and stays human; operator_name() then falls back to it.
        _warn_posture_once(
            "operator_worker_tokens",
            "ua-audit: [Warning] cannot read UA_AGENT_TOKENS "
            f"({type(exc).__name__}); operator names that need that check are refused",
        )
        return True
    if any(label.casefold() == key for label, _ in token_pairs):
        return True
    if key in _pinned_oauth_worker_labels():
        return True
    return _managed_worker_label(key)


def _pinned_oauth_worker_labels() -> frozenset[str]:
    """Casefolded labels the configuration ties to OAuth or WorkOS authentication.

    These authenticate as a roster identity without an ``agent_tokens`` row:
    pinned OAuth clients (``UA_MCP_OAUTH_CLIENTS``), non-interactive
    client-credentials (``UA_MCP_OAUTH_CLIENT_CREDENTIALS``), the default OAuth
    subject (``UA_MCP_OAUTH_AGENT``) and the WorkOS subject-to-label map
    (``UA_HUB_WORKOS_IDENTITY_MAP``). Labels only: no secret is parsed out.
    Fails closed: an unreadable or malformed value names no one, but a label
    that is present in any readable form is a worker (task #1760).

    Both the process environment and the hub's ``$UA_HOME/service.env`` are
    read, and their labels are united (task #1971). The hub process has
    service.env loaded, but ``unified-agent init`` and other CLIs do not, and
    they must not accept as a human alias a label the hub authenticates as a
    worker.
    """
    from .service_env import parse_env_file

    labels = _oauth_worker_labels_from(os.environ)
    labels |= _oauth_worker_labels_from(parse_env_file(data_home() / "service.env"))
    return frozenset(label.strip().casefold() for label in labels if label.strip())


def _oauth_worker_labels_from(source: Mapping[str, str]) -> set[str]:
    """The raw pinned OAuth/WorkOS labels one configuration source declares."""
    labels: set[str] = set()
    explicit = (source.get("UA_MCP_OAUTH_AGENT") or "").strip()
    if explicit:
        labels.add(explicit)
    # Only entries their own parsers would accept: a pin with an empty client
    # id or secret cannot authenticate, so it must not withhold a human name
    # (Devin review, PR #978). The known-label check those parsers add is left
    # out on purpose: it consults operator classification, which calls here.
    # An empty value parses as no pins; a None would mean "read os.environ".
    try:
        labels.update(
            agent for agent, _, _ in _oauth_client_pins(source.get("UA_MCP_OAUTH_CLIENTS") or "")
        )
    except ValueError:
        # An unreadable client file cannot prove its labels are human; the
        # pins are also unusable, so there is no worker label to withhold.
        pass
    raw = (source.get("UA_MCP_OAUTH_CLIENT_CREDENTIALS") or "").strip()
    if raw:
        if raw.startswith(("/", "~")) or os.path.exists(raw):
            try:
                raw = Path(raw).expanduser().read_text()
            except (OSError, UnicodeError):
                raw = ""
        labels.update(agent for _, _, agent in _client_credential_triples(raw))
    try:
        mapping = json.loads((source.get("UA_HUB_WORKOS_IDENTITY_MAP") or "").strip() or "{}")
    except ValueError:
        mapping = {}
    if isinstance(mapping, dict):
        labels.update(v for v in mapping.values() if isinstance(v, str))
    return labels


# Managed-credential label lookups are cached per committed store version.
# ``PRAGMA data_version`` on a long-lived read-only connection changes whenever
# any other connection commits, so a newly issued worker token always misses
# the cache. It only saves repeated lookups between writes, such as one per
# row while building a task graph.
_MANAGED_WORKER_LOCK = threading.Lock()
# One held connection: (path, inode, conn). data_version numbers restart on a
# new connection, so every (re)open also clears the cache; otherwise a
# reconnect could reuse a version number cached at an older store state.
_MANAGED_WORKER_CONN: list = []
_MANAGED_WORKER_CACHE: dict[tuple, bool] = {}
_MANAGED_WORKER_CACHE_MAX = 512


def _utc_now_iso() -> str:
    # storage.now()'s format, which agent_tokens.expires_at is compared against.
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _drop_managed_worker_conn() -> None:
    """Close the held connection and forget every result cached through it."""
    if _MANAGED_WORKER_CONN:
        try:
            _MANAGED_WORKER_CONN[2].close()
        except Exception:  # noqa: BLE001 - closing a broken handle must not raise
            pass
    _MANAGED_WORKER_CONN.clear()
    _MANAGED_WORKER_CACHE.clear()


def _store_names_worker(conn, key: str, tenant) -> bool:
    """Whether the credential store ties ``key`` to a live worker credential.

    Covers managed bearers (``agent_tokens``) and OAuth grants: the label an
    access or refresh token authenticates as lives in ``oauth_*_tokens.subject``.
    A missing table means that mechanism was never installed, so it holds no
    worker. Any other store error propagates to the caller, which fails closed.
    """
    import sqlite3
    import time

    now = _utc_now_iso()
    try:
        # Same liveness rule as agent_tokens(): an expired credential no
        # longer authenticates, so it no longer owns the name. A cached
        # True can outlive an expiry until the next commit; that only
        # delays accepting the name, never grants it early.
        try:
            row = conn.execute(
                "SELECT 1 FROM agent_tokens WHERE lower(agent_label) = ? "
                "AND org_id IS ? AND status = 'active' "
                "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (key, tenant, now),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column: expires_at" not in str(exc):
                raise
            row = conn.execute(
                "SELECT 1 FROM agent_tokens WHERE lower(agent_label) = ? "
                "AND org_id IS ? AND status = 'active' LIMIT 1",
                (key, tenant),
            ).fetchone()
        if row is not None:
            return True
    except sqlite3.OperationalError as exc:
        if "no such table: agent_tokens" not in str(exc):
            raise
    # OAuth grants (task #1760): a roster agent that signs in through OAuth has
    # no agent_tokens row. expires_at is epoch seconds and NULL means no expiry.
    epoch = int(time.time())
    for table in ("oauth_access_tokens", "oauth_refresh_tokens"):
        try:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE lower(subject) = ? "
                "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (key, epoch),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if f"no such table: {table}" not in str(exc):
                raise
            continue
        if row is not None:
            return True
    return False


def _managed_worker_label(key: str) -> bool:
    """Whether an active managed bearer or live OAuth grant carries ``key``."""
    import sqlite3

    from . import edition, org_partition

    path = Path(db_path())
    try:
        inode = path.stat().st_ino
    except OSError:
        return False
    tenant = edition.tenant_org_id()
    with _MANAGED_WORKER_LOCK:
        try:
            if not _MANAGED_WORKER_CONN or _MANAGED_WORKER_CONN[:2] != [str(path), inode]:
                _drop_managed_worker_conn()
                # Label-only metadata, read-only: no initialization, migration
                # or secret decryption.
                conn = org_partition.open_store(
                    path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
                )
                _MANAGED_WORKER_CONN[:] = [str(path), inode, conn]
            else:
                conn = _MANAGED_WORKER_CONN[2]
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            cache_key = (key, tenant, version)
            cached = _MANAGED_WORKER_CACHE.get(cache_key)
            if cached is not None:
                return cached
            result = _store_names_worker(conn, key, tenant)
        except sqlite3.Error as exc:
            _drop_managed_worker_conn()
            # Fail closed without failing the request: an unreadable store
            # cannot prove a name is human, so it is treated as a worker.
            _warn_posture_once(
                "operator_worker_lookup",
                "ua-audit: [Warning] cannot read managed agent credentials "
                f"({type(exc).__name__}); operator names that need that check are refused",
            )
            return True
        if len(_MANAGED_WORKER_CACHE) >= _MANAGED_WORKER_CACHE_MAX:
            _MANAGED_WORKER_CACHE.clear()
        _MANAGED_WORKER_CACHE[cache_key] = result
        return result


def operator_label_problem(raw: str) -> str | None:
    """Why ``raw`` cannot be the operator's login label, or None if it can.

    Shared by ``operator_label()`` and ``unified-agent init`` so init refuses
    exactly what the hub would ignore at runtime.
    """
    if not agent_label_is_well_formed(raw):
        return "is not a well-formed label"
    if _names_worker_identity(raw):
        # Never let a configured human label alias an agent identity: the
        # operator login gets the OPERATOR profile and human authority.
        return "names a catalogued agent identity or a configured worker"
    return None


def operator_name() -> str:
    """The human operator's bare name.

    UA_OPERATOR_NAME, else the first UA_OPERATOR_ALIASES entry, else
    ``Operator``. The fallback matters because UA_OPERATOR_NAME is also an
    evidence input (audit.evidence_report): a deployment can name its human
    through aliases without asserting an operator claim on every record.
    """
    candidates = [(os.environ.get("UA_OPERATOR_NAME") or "").strip()]
    candidates += [a.strip() for a in (os.environ.get("UA_OPERATOR_ALIASES") or "").split(",")]
    for name in candidates:
        if name and not _names_worker_identity(name, include_roster=False):
            return name
    if _names_worker_identity(DEFAULT_OPERATOR_NAME, include_roster=False):
        return operator_label()
    return DEFAULT_OPERATOR_NAME


def operator_label() -> str:
    """The human operator's login identity (UA_OPERATOR_LABEL)."""
    raw = (os.environ.get("UA_OPERATOR_LABEL") or "").strip()
    if raw:
        problem = operator_label_problem(raw)
        if problem is None:
            return raw
        _warn_posture_once(
            "operator_label",
            f"ua-audit: [Warning] UA_OPERATOR_LABEL {raw!r} {problem}; "
            f"using {DEFAULT_OPERATOR_LABEL!r}",
        )
    problem = operator_label_problem(DEFAULT_OPERATOR_LABEL)
    if problem is not None:
        # The default itself names a worker. No login may be human then, but
        # this runs on every request, so refuse by returning a value no bearer
        # or session can resolve to rather than raising.
        _warn_posture_once(
            "operator_label_default",
            f"ua-audit: [Warning] default operator label {DEFAULT_OPERATOR_LABEL!r} "
            f"{problem}; no login is treated as the human operator until "
            "UA_OPERATOR_LABEL names a separate human login label",
        )
        return UNASSIGNED_OPERATOR_LABEL
    return DEFAULT_OPERATOR_LABEL


def operator_aliases() -> frozenset[str]:
    """Claimed names that stand for the operator: ``name`` plus UA_OPERATOR_ALIASES.

    An alias naming a catalogued or credential-backed worker is dropped with
    a warning:
    aliases are classified human, so accepting one would hand that agent's
    bearer human-only authority such as approving.
    """
    names = {operator_name()}
    raw = os.environ.get("UA_OPERATOR_ALIASES", "")
    names.update(part.strip() for part in raw.split(",") if part.strip())
    names.discard(operator_label())
    refused = {name for name in names if _names_worker_identity(name, include_roster=False)}
    if refused:
        _warn_posture_once(
            "operator_aliases",
            "ua-audit: [Warning] UA_OPERATOR_ALIASES names catalogued or configured agent "
            f"identities {sorted(refused)!r}; ignoring them",
        )
    return frozenset(names - refused)


def identity_alias_allowlist() -> dict[str, frozenset[str]]:
    """Narrow, auditable (verified -> claimed) pairs for the mismatch check.

    The operator's login label (memory 92437723a6e8, 2026-07-30) authenticates
    and resolves an identity like anyone else, but tools such as `delegate`
    that require a *known* from_agent label have no notion of a human
    operator. This allowlist lets that login stand in for the operator's own
    claimed names — and nothing else. It is not a general impersonation
    exemption: any pair not listed still hits the mismatch branch.
    """
    return {operator_label(): operator_aliases()}


def operator_dispatch_alias(identity: str) -> str:
    """Attribute work from the operator's login to their registered human name.

    The Control Center login is not a roster agent label, so a delegation or
    crew intervention it originates is stamped with ``operator_name()``.
    Any other identity passes through unchanged.
    """
    return operator_name() if identity == operator_label() else identity


def __getattr__(name: str):
    # Back-compat for callers that read the former module constants. They are
    # now derived from the operator settings on every access.
    if name == "IDENTITY_ALIAS_ALLOWLIST":
        return identity_alias_allowlist()
    if name == "HUMAN_IDENTITY_LABELS":
        return human_identity_labels()
    if name == "FLEET_AGENT_LABELS":
        return fleet_agent_labels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def verify_write_identity(claimed_author: str | None) -> str | None:
    """Verifies that the claimed author matches the verified agent identity if authenticated.

    Returns the verified identity to use/stamp — or, for an allowlisted
    (verified, claimed) pair, the claimed identity instead (see
    identity_alias_allowlist()). Raises ToolError on an unlisted mismatch in
    enforce mode. With no verified identity, HTTP and
    ``UA_REQUIRE_CALLER_IDENTITY=1`` return None; stdio otherwise returns the
    claimed author.
    """
    verified = agent_identity_var.get()
    if verified is None:
        # HTTP never stamps an arbitrary claimed author: there is no verified
        # caller to bind it to. Stdio satellites keep the historical trust
        # unless UA_REQUIRE_CALLER_IDENTITY=1. UA_ENFORCE_AUTH=1 on stdio does
        # not by itself drop the claim — that would break every satellite that
        # passes an author with no bearer middleware. See docs/security-posture.md.
        if transport() == "http" or require_caller_identity():
            return None
        return claimed_author

    if claimed_author is not None and claimed_author != verified:
        if claimed_author in identity_alias_allowlist().get(verified, ()):
            return claimed_author

        enforce = enforce_auth()
        msg = f"Auth identity mismatch: claimed {claimed_author!r} but verified as {verified!r}"
        if enforce:
            from mcp.server.mcpserver.exceptions import ToolError

            raise ToolError(msg)
        else:
            import sys

            print(f"ua-audit: [Warning] {msg}", file=sys.stderr)

    return verified


def _parse_colon_pairs(env_var_name: str, raw: str | None = None) -> list[tuple[str, str]]:
    """Shared parser for UA_AGENT_TOKENS/UA_AGENT_WORKSPACES-style config.

    Reads `env_var_name`: either a comma/newline-separated list of `left:right`
    entries, or a file path containing them. Returns the parsed (left, right)
    pairs in order; malformed entries (no colon, empty half) are silently
    dropped so a typo doesn't disable the whole allowlist. A value that looks
    like a path (starts with ``/`` or ``~``, or names an existing path) but is
    missing or unreadable raises ValueError — swallowing that used to yield an
    empty map and fail open under ``UA_ENFORCE_AUTH``. An empty env value is
    not a path and still returns no pairs. Callers build whichever dict shape
    (and key order) they need from the pairs.
    """
    # `raw` lets a caller parse a value it resolved elsewhere — system_doctor
    # reads the SERVICE's configuration from its systemd unit rather than from
    # its own process env (task #176). Inline pairs are unchanged; a path-like
    # value that cannot be read raises instead of parsing as no pairs.
    raw = (raw if raw is not None else os.environ.get(env_var_name, "")).strip()
    if not raw:
        return []

    looks_like_path = raw.startswith("/") or raw.startswith("~") or os.path.exists(raw)
    if looks_like_path:
        path = Path(raw).expanduser()
        try:
            if not path.is_file():
                raise OSError(f"{path} is not a file")
            raw = path.read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"{env_var_name} looks like a path ({raw!r}) but the file is "
                f"missing or unreadable: {exc}"
            ) from exc

    entries = [line.strip() for line in raw.replace("\n", ",").split(",") if line.strip()]

    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if ":" not in entry:
            continue
        left, _, right = entry.partition(":")
        left, right = left.strip(), right.strip()
        if left and right:
            pairs.append((left, right))
    return pairs


def require_configured_http_credentials() -> None:
    """Refuse to start an HTTP hub that enforces auth with no credential leg.

    Called from ``server.main`` on the hub-serve path, immediately before
    ``ensure_dirs``. Stdio is unchanged: satellites may run with
    ``UA_ENFORCE_AUTH=1`` and no tokens. An empty ``UA_AGENT_TOKENS`` is not
    an error on its own. A path-like value that cannot be read raises from
    ``_parse_colon_pairs`` and is reported here as a startup failure, so a
    typo never becomes an empty token map that the hub then serves anonymously.

    Credential legs match the hub middleware: bearer tokens, an OAuth provider
    (``mcp_oauth_enabled``, which is exactly when ``oauth.get_provider()`` is
    not None), or WorkOS hub auth enabled. An explicit ``UA_ENFORCE_AUTH`` off
    token still starts, and warns: anonymous clients can call tools. Unset on
    HTTP enforces and is not that warn mode.
    """
    if transport() != "http":
        return
    if not enforce_auth():
        if _env_token("UA_ENFORCE_AUTH") is not None:
            _warn_posture_once(
                "http-auth-disabled",
                "ua-audit: [Warning] UA_TRANSPORT=http but UA_ENFORCE_AUTH is off: "
                "anonymous clients can list and call tools. Unset UA_ENFORCE_AUTH "
                "(HTTP then enforces) or set it to 1.",
            )
        return

    import sys

    try:
        tokens = agent_tokens()
    except ValueError as exc:
        print(f"ua: refusing to start: {exc}", file=sys.stderr)
        sys.exit(1)

    from . import identity_core

    if tokens or mcp_oauth_enabled() or identity_core.enabled():
        return

    print(
        "ua: refusing to start: UA_ENFORCE_AUTH=1 but no credential leg is "
        "configured (UA_AGENT_TOKENS is empty, OAuth is off, and WorkOS hub "
        "auth is not enabled). Point UA_AGENT_TOKENS at a readable file or "
        "inline label:token pairs, configure OAuth or WorkOS, or set "
        "UA_ENFORCE_AUTH=0 to keep warn-only mode.",
        file=sys.stderr,
    )
    sys.exit(1)


def agent_token_entries(
    raw: str | None, include_managed: bool | None
) -> list[tuple[str, str, str]]:
    """Every (label, token, source) pair ``agent_tokens`` merges, before the
    collision rule. ``source`` is ``UA_AGENT_TOKENS`` or ``managed``."""
    entries: list[tuple[str, str, str]] = [
        (label, tok, "UA_AGENT_TOKENS") for label, tok in _parse_colon_pairs("UA_AGENT_TOKENS", raw)
    ]
    should_include = (raw is None) if include_managed is None else include_managed
    if should_include:
        try:
            from . import edition, managed_secrets, storage

            if Path(db_path()).is_file():
                conn = storage.connect()
                try:
                    now = storage.now()
                    current_org = edition.tenant_org_id()
                    rows = conn.execute(
                        "SELECT agent_label, token_hash, hash_algo, org_id, expires_at "
                        "FROM agent_tokens "
                        "WHERE status = 'active' AND org_id IS ? AND (expires_at IS NULL OR expires_at > ?)",
                        (current_org, now),
                    ).fetchall()
                finally:
                    conn.close()
                for row in rows:
                    label = row["agent_label"]
                    org_id = row["org_id"]
                    expected_hash = row["token_hash"]
                    if not agent_label_is_well_formed(label):
                        continue
                    try:
                        secret_name = f"agent-token:{label}"
                        val = managed_secrets._load_for_consumer(
                            secret_name, consumer="agent_token", org_id=org_id
                        )
                        if val and isinstance(val, str):
                            digest = hashlib.sha256(val.encode("utf-8")).hexdigest()
                            if digest == expected_hash:
                                entries.append((label, val, "managed"))
                    except Exception:
                        pass
        except Exception:
            pass
    return entries


def agent_token_collisions(
    raw: str | None = None, *, include_managed: bool | None = None
) -> list[dict[str, object]]:
    """Groups of labels that share one token, which ``agent_tokens`` fails closed on.

    Labels and sources only, never a token value (task #1657). A group with
    more than one source is the confusing case: a client reads its bearer from
    its own environment, so it looks configured, while the hub refuses it.
    Same inputs and same reads as ``agent_tokens``; prints nothing.
    """
    by_token: dict[str, dict[str, set[str]]] = {}
    for label, tok, source in agent_token_entries(raw, include_managed):
        by_token.setdefault(tok, {}).setdefault(label, set()).add(source)
    groups: list[dict[str, object]] = []
    for labels in by_token.values():
        if len(labels) < 2:
            continue
        sources = sorted(set().union(*labels.values()))
        groups.append(
            {
                "labels": sorted(labels),
                "sources": sources,
                "cross_source": len(sources) > 1,
            }
        )
    return sorted(groups, key=lambda group: group["labels"])


def agent_tokens(raw: str | None = None, *, include_managed: bool | None = None) -> dict[str, str]:
    """Parse UA_AGENT_TOKENS from the environment into a dict of {token: agent_label}.

    Supports a comma-separated list of agent_label:token or a file path containing them.
    Also merges active, non-expired credentials from the managed secrets store.

    Fail-closed on a token collision (task #299): if the same token string is
    listed for two or more distinct labels — a duplicate/stale registration,
    e.g. a rotated token whose old entry was never removed and got reassigned —
    that token authenticates *no one* rather than silently resolving to
    whichever label happened to parse last. A caller presenting a collided
    token is treated exactly like an invalid token.
    """
    token_to_labels: dict[str, set[str]] = {}
    for label, tok, _source in agent_token_entries(raw, include_managed):
        token_to_labels.setdefault(tok, set()).add(label)

    result: dict[str, str] = {}
    for tok, labels in token_to_labels.items():
        if len(labels) > 1:
            import sys

            print(
                f"ua-audit: [Warning] token collision: {sorted(labels)} "
                "share one token; failing closed — this token authenticates no "
                "identity until the duplicate registration is resolved.",
                file=sys.stderr,
            )
            continue
        result[tok] = next(iter(labels))

    return result


def agent_scopes(raw: str | None = None) -> dict[str, frozenset[str]]:
    """Parse ``UA_AGENT_SCOPES`` into ``{agent_label: action scopes}``.

    Entries use ``agent:scope|scope`` (comma/newline separated). Conflicting
    duplicate labels fail closed instead of trusting the last entry.
    """
    result: dict[str, frozenset[str]] = {}
    seen: dict[str, frozenset[str]] = {}
    conflicts: set[str] = set()
    value = raw if raw is not None else os.environ.get("UA_AGENT_SCOPES", "")
    pairs = _parse_colon_pairs("UA_AGENT_SCOPES", value)
    if value.strip() and not pairs:
        # Fail closed: a non-empty value that parses to nothing would silently
        # turn scope enforcement off (empty mapping = nobody is restricted).
        raise ValueError(
            "UA_AGENT_SCOPES is set but contains no valid entries; "
            "refusing to run with scope enforcement disabled"
        )
    for label, scope_text in pairs:
        scopes = frozenset(item.strip().lower() for item in scope_text.split("|") if item.strip())
        if label in seen and seen[label] != scopes:
            conflicts.add(label)
        seen[label] = scopes
        result[label] = scopes
    for label in conflicts:
        result[label] = frozenset()
    return result


def revoked_agents(raw: str | None = None) -> frozenset[str]:
    """Return agent labels revoked from all governed actions."""
    value = raw if raw is not None else os.environ.get("UA_REVOKED_AGENTS", "")
    return frozenset(
        item.strip()
        for item in value.replace("\n", ",").replace(":", ",").split(",")
        if item.strip()
    )


def agent_hosts(raw: str | None = None) -> dict[str, str]:
    """Parse UA_AGENT_HOSTS into {agent_label: host} (task #1388).

    Same ``label:host`` format as UA_AGENT_WORKSPACES: an explicit binding of
    an agent label to the machine it runs on, used when no ua-node reports the
    label. A label listed twice with different hosts is dropped (neither value
    is trusted), matching the fail-closed duplicate rule of the other maps.
    """
    seen: dict[str, str] = {}
    conflicted: set[str] = set()
    for label, host in _parse_colon_pairs("UA_AGENT_HOSTS", raw):
        host = host.strip()
        if label in seen and seen[label] != host:
            conflicted.add(label)
        seen[label] = host
    return {label: host for label, host in seen.items() if label not in conflicted and host}


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _looks_like_machine_name(name: str) -> bool:
    """A configured host value specific enough to redact on sight.

    A bare word such as ``desktop`` would redact ordinary prose, so a derived
    name must carry a dot, a hyphen or a digit. Names an operator lists in
    ``UA_RELAY_INTERNAL_HOSTNAMES`` skip this check.
    """
    if len(name) < 3 or name.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return False
    return any(ch in name for ch in ".-") or any(ch.isdigit() for ch in name)


def relay_internal_hostnames() -> frozenset[str]:
    """Machine names the public-reply relay redacts from outbound drafts (task #1734).

    The union of ``UA_RELAY_INTERNAL_HOSTNAMES`` (comma or newline separated,
    taken as given) and names this hub already knows from its own config:
    ``UA_AGENT_HOSTS`` values, ``UA_HTTP_ALLOWED_HOSTS`` (ports stripped), the
    vault-sync SSH host and this machine's hostname. Nothing is hardcoded:
    the gate learns names from configuration, never from a list in source.
    """
    names = {name for name in _split_list(os.environ.get("UA_RELAY_INTERNAL_HOSTNAMES", ""))}
    derived: list[str] = list(agent_hosts().values())
    derived.extend(
        host.rsplit(":", 1)[0] if host.count(":") == 1 else host for host in http_allowed_hosts()
    )
    try:
        derived.append(vault_sync_remote_host())
    except Exception:  # pragma: no cover - defensive: config read must not break gating
        pass
    try:
        derived.append(_default_source_host())
    except Exception:  # pragma: no cover
        pass
    for host in derived:
        host = (host or "").strip()
        if host and _looks_like_machine_name(host):
            names.add(host)
            short = host.split(".", 1)[0]
            if short != host and _looks_like_machine_name(short):
                names.add(short)
    return frozenset(name for name in names if len(name) >= 3)


def relay_blocked_terms() -> frozenset[str]:
    """Operator-listed terms that block a public-reply draft (task #1734).

    ``UA_RELAY_BLOCKED_TERMS``, comma or newline separated, matched as whole
    words without regard to case. Empty by default: the policy list is
    configuration, not source.
    """
    return frozenset(
        term.lower() for term in _split_list(os.environ.get("UA_RELAY_BLOCKED_TERMS", ""))
    )


# Surface identities belong to the installation, not the shipped product.
# Preserve existing bindings explicitly in UA_SURFACE_MAP before upgrading.
DEFAULT_FLEET_SURFACE_MAP: dict[str, str] = {}


def _parse_surface_pairs(raw: str | None = None) -> list[tuple[str, str]]:
    raw_val = os.environ.get("UA_SURFACE_MAP", "") if raw is None else raw
    if not raw_val:
        return []
    entries = [line.strip() for line in raw_val.replace("\n", ",").split(",") if line.strip()]
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if ":" not in entry:
            continue
        left, _, right = entry.partition(":")
        left, right = left.strip(), right.strip()
        if left:
            pairs.append((left, right))
    return pairs


def surface_map(raw: str | None = None) -> dict[str, str]:
    """Parse UA_SURFACE_MAP into {"<host>/<kind>/<name>": agent_label} (task #1391, #1538).

    ``kind`` is ``hermes`` (a Hermes profile) or ``kanban`` (a kanban assignee).
    Entries use the ``left:right`` format of UA_AGENT_WORKSPACES, e.g.
    ``Desktop/hermes/default:Desktop_Agent5``. A key listed twice with
    different labels is dropped.

    No installation bindings are compiled in. When ``raw`` is None, active
    ``UA_SURFACE_MAP`` supplies the mapping. Conflicting labels for a key drop
    that key entirely (fail-closed). An empty label or "NONE" suppresses it.
    """
    if raw is not None:
        seen: dict[str, str] = {}
        conflicted: set[str] = set()
        for key, label in _parse_surface_pairs(raw):
            if key in seen and seen[key] != label:
                conflicted.add(key)
            seen[key] = label
        return {k: v for k, v in seen.items() if k not in conflicted and v and v.upper() != "NONE"}

    base = dict(DEFAULT_FLEET_SURFACE_MAP)
    seen: dict[str, str] = {}
    conflicted: set[str] = set()
    for key, label in _parse_surface_pairs(None):
        if key in seen and seen[key] != label:
            conflicted.add(key)
        seen[key] = label

    # Remove all conflicted keys from base (fail-closed)
    for key in conflicted:
        base.pop(key, None)

    # Apply overrides (an empty label or "NONE" explicitly deletes/suppresses a default entry)
    for key, label in seen.items():
        if key not in conflicted:
            if not label or label.upper() == "NONE":
                base.pop(key, None)
            else:
                base[key] = label

    return base


def agent_workspaces() -> dict[str, Path | WorkspaceDenied]:
    """Parse UA_AGENT_WORKSPACES into {agent_label: workspace_root | WORKSPACE_DENIED}.

    Same format as UA_AGENT_TOKENS (comma/newline-separated label:path entries, or
    a file path containing them). Lets the shared HTTP hub bind a per-agent
    workspace root via the same verified identity used for auth (task #98),
    so a satellite project can use the hub instead of spawning its own stdio
    process with a fixed UA_WORKSPACE. Purely additive: an agent with no entry
    here keeps falling back to the process-wide UA_WORKSPACE env var,
    unchanged — but see ``resolve_workspace_binding()`` for the fail-closed
    policy device-scoped identities get layered on top of this raw mapping.

    A value of "DENY" (case-insensitive) maps to WORKSPACE_DENIED instead of a
    path — for identities that must never get hub-mediated fs_*/run_cmd/git_*
    access at all (e.g. Laptop_* clients, whose dev-repo work happens through
    their own native file tools). This is deliberately distinct from having no
    entry, which falls through to the shared UA_WORKSPACE root.

    Fail-closed on a duplicate/stale registration (task #299): if the same
    label appears more than once with conflicting values (e.g. a stale "DENY"
    left in place alongside a newer real path, or two different paths typo'd
    in), that label resolves to WORKSPACE_DENIED rather than trusting
    whichever entry parsed last. A repeated entry with the *same* value is not
    a conflict.
    """
    pairs = _parse_colon_pairs("UA_AGENT_WORKSPACES")
    seen_raw: dict[str, str] = {}
    conflicted: set[str] = set()
    result: dict[str, Path | WorkspaceDenied] = {}
    for label, path_str in pairs:
        normalized = path_str.strip()
        value = WORKSPACE_DENIED if normalized.upper() == "DENY" else Path(normalized).expanduser()
        if label in seen_raw and seen_raw[label] != normalized:
            conflicted.add(label)
        seen_raw[label] = normalized
        result[label] = value

    for label in conflicted:
        print(
            f"ua-audit: [Warning] UA_AGENT_WORKSPACES has conflicting duplicate "
            f"entries for {label!r}; failing closed (denied) rather than trusting "
            "the last-parsed value.",
            file=sys.stderr,
        )
        result[label] = WORKSPACE_DENIED

    return result


def explicit_workspace_deny(agent_label: str) -> bool:
    """True when UA_AGENT_WORKSPACES has a solitary intentional DENY for label.

    Conflicting duplicate entries also surface as ``WorkspaceDenied`` (fail
    closed), but those are *not* an explicit DENY — callers that treat DENY as
    a spawn-cwd fallback (Desktop CLI on the hub host) must not apply that
    fallback to a conflict.
    """
    pairs = _parse_colon_pairs("UA_AGENT_WORKSPACES")
    values = [path_str.strip() for label, path_str in pairs if label == agent_label]
    if not values:
        return False
    distinct = set(values)
    if len(distinct) > 1:
        return False
    return next(iter(distinct)).upper() == "DENY"


# Identities scoped to a specific, non-desktop device. Their dev-repo work
# happens through their own native file tools, never through the shared
# desktop hub's fs_*/run_cmd sandbox — see docs/desktop-migration.md: the hub
# process runs on the desktop and has no filesystem access to any other
# machine, Tailscale or not. This prefix identifies identities for which an
# *unbound* UA_AGENT_WORKSPACES entry must fail closed instead of silently
# inheriting the desktop's own workspace root.
DEVICE_SCOPED_IDENTITY_PREFIXES = ("Laptop_", "server_")


def _is_device_scoped_identity(agent_label: str) -> bool:
    return agent_label.startswith(DEVICE_SCOPED_IDENTITY_PREFIXES)


# Identities hosted on a third-party cloud (task #1799). ``cloud_agent13`` is
# the chief of staff. ``cloud_agent8`` remains on this roster (live tokens and
# sessions must keep resolving) and defaults to research. ``cloud_agent17`` is a
# developer (operator decision 2026-11-11; it was the backup chief of staff).
# Each runs on a provider-hosted computer and drives local work through its
# own connected-computer tooling, never through the hub host's filesystem. A
# profile change must not drop the boundaries, so they key on this roster
# rather than on the profile name: hub-side workspace access is always denied,
# Control Center routes stay closed, and vault writes keep the cloud lock,
# force, journal and write-dir guards. Adding a label here is a security
# decision, not a convenience.
#
# ``cloud_agent1`` (task #2446) is Claude Code on the web: an Anthropic-hosted VM
# that holds its own clone of the repo and reaches the hub's /mcp over
# Tailscale (the tailnet) with a client-credentials grant. It is a developer,
# not a chief of staff, and every boundary above applies to it unchanged: it
# edits code in its own VM and opens PRs, never touches the hub host's disk.
CLOUD_HOSTED_IDENTITIES = frozenset(
    {
        "cloud_agent1",
        "cloud_agent17",
        "cloud_agent16",
        "cloud_agent14",
        "cloud_agent8",
        "cloud_agent15",
        "cloud_agent13",
    }
)


def is_cloud_hosted_identity(agent_label: str | None) -> bool:
    """Whether a verified identity runs on a third-party cloud host.

    The fixed roster above, plus any label an operator added with
    ``unified-agent agent add --kind cloud`` (task #2453): the same
    boundaries apply to it. That kind is read from the label's newest
    ``agent_tokens`` row that records one, on every call and in the same
    database the bearer is read from, so it holds from the first request the
    bearer makes. The roster's seat (cached per process) is consulted too; it
    can only add the boundary, never lift it.
    """
    if not agent_label:
        return False
    if agent_label in CLOUD_HOSTED_IDENTITIES:
        return True
    if _token_row_kind(agent_label) == "cloud":
        return True
    try:
        from . import agent_roster

        return agent_label in agent_roster.cloud_labels()
    except Exception:  # noqa: BLE001 - an added agent's bearer lives in the same
        # database, so a roster that cannot be read cannot authenticate it either.
        return False


def _token_row_kind(agent_label: str) -> str | None:
    """``agent_kind`` on ``agent_label``'s newest token row that records one, uncached.

    None without a hub database, a table or the column (none of which can
    hold an operator-added bearer). Any other store error is ``"cloud"``: the
    caller decides whether a label gets the hub's workspace, and an unreadable
    row must not grant it.
    """
    import sqlite3

    if not agent_label_is_well_formed(agent_label) or not Path(db_path()).is_file():
        return None
    try:
        from . import edition, storage

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT agent_kind FROM agent_tokens WHERE agent_label = ? COLLATE NOCASE "
                "AND org_id IS ? AND agent_kind IS NOT NULL ORDER BY id DESC LIMIT 1",
                (agent_label, edition.tenant_org_id()),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "no such table: agent_tokens" in text or "no such column: agent_kind" in text:
            return None
        return "cloud"
    except Exception:  # noqa: BLE001 - fail toward the narrower boundary
        return "cloud"
    return row["agent_kind"] if row is not None else None


def resolve_workspace_binding(agent_label: str) -> Path | WorkspaceDenied | None:
    """Resolve the per-request hub workspace binding for a verified identity (task #299).

    Wraps ``agent_workspaces()`` with the fail-closed policy device-scoped
    identities need: a ``Laptop_*`` identity with no explicit
    UA_AGENT_WORKSPACES entry must never silently inherit the shared
    UA_WORKSPACE root — that root is the desktop's own filesystem — the way a
    Desktop_*/shared identity intentionally does today. A missing entry for a
    device-scoped identity resolves to WORKSPACE_DENIED, exactly like an
    explicit "DENY" entry, instead of falling through.

    Returns ``None`` only when it is safe to fall through to the process-wide
    UA_WORKSPACE default: the identity is not device-scoped and has no entry.
    """
    mapping = agent_workspaces()
    if is_cloud_hosted_identity(agent_label):
        # Not configurable: a cloud-hosted identity has no business on the hub
        # host's disk, so a missing or path-valued entry cannot grant it
        # fs_*/run_cmd/git_* reach. Keyed on identity so that a wider profile
        # (chief-of-staff exposes every tool) never reopens the workspace.
        if mapping.get(agent_label, WORKSPACE_DENIED) is not WORKSPACE_DENIED:
            import sys

            print(
                f"ua-audit: [Warning] cloud-hosted identity {agent_label!r} has a "
                "UA_AGENT_WORKSPACES path entry; ignoring it and denying hub "
                "workspace access.",
                file=sys.stderr,
            )
        return WORKSPACE_DENIED
    if agent_label in mapping:
        return mapping[agent_label]
    if _is_device_scoped_identity(agent_label):
        import sys

        print(
            f"ua-audit: [Warning] device-scoped identity {agent_label!r} has no "
            "UA_AGENT_WORKSPACES entry; failing closed (denied) rather than "
            "falling through to the shared UA_WORKSPACE root.",
            file=sys.stderr,
        )
        return WORKSPACE_DENIED
    return None


def state_sync_debounce_seconds() -> float:
    """Debounce window (seconds) for coalescing vault state snapshots.

    A burst of rapid mutations collapses into a single snapshot written this
    many seconds after the last mutation. ``0`` (or a non-positive value)
    disables debouncing, restoring synchronous writes — useful for tests.
    Default 3.0s (within the 2-5s design range).
    """
    try:
        val = float(os.environ.get("UA_STATE_SYNC_DEBOUNCE", "3.0"))
    except (TypeError, ValueError):
        return 3.0
    return max(0.0, val)


def plan_max_retries() -> int:
    """Retry cap for a single plan step before it escalates to the human (task #61).

    A misconfigured value must not disable the guard, so a non-integer or
    non-positive setting falls back to the default of 3 bounded retries.
    """
    raw = os.environ.get("UA_PLAN_MAX_RETRIES", "3")
    try:
        n = int(raw)
    except ValueError:
        return 3
    return n if n >= 1 else 3


def plan_escalation_channel() -> str:
    """Where a step that exhausts its retries is surfaced (task #61).

    Storage-only: these primitives never send an iMessage or write elsewhere;
    the channel is recorded on the plan so a caller/steward can act on it. The
    default 'flag' means the escalation is only the DB flag (`plans.escalated`).
    """
    return os.environ.get("UA_PLAN_ESCALATION_CHANNEL", "flag")


def autonomy_budget_defaults() -> dict[str, float | int]:
    """Default per-cycle budgets; invalid or non-positive values use safe caps."""
    defaults: dict[str, float | int] = {
        "time_s": 3600.0,
        "tokens": 100_000,
        "retries": 3,
        "cost_cents": 1000.0,
    }
    env_names = {
        "time_s": "UA_AUTONOMY_MAX_TIME_S",
        "tokens": "UA_AUTONOMY_MAX_TOKENS",
        "retries": "UA_AUTONOMY_MAX_RETRIES",
        "cost_cents": "UA_AUTONOMY_MAX_COST_CENTS",
    }
    for key, env_name in env_names.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            value = float(raw) if key in {"time_s", "cost_cents"} else int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            defaults[key] = value
    return defaults


# Empty on purpose: a third-party install must not inherit this fleet's names.
# Set UA_AGENT_LABELS to close the roster. Unset/empty = open roster (custom
# well-formed labels are accepted; Desktop_/Laptop_/server_ names must match
# FLEET_AGENT_LABELS, case-insensitively — desktop_agent2 is Desktop_Agent2).
DEFAULT_AGENT_LABELS = ""

# Starts with a letter; letters, digits, underscore, hyphen; max 64 chars.
# Rejects empty, whitespace, colons (env-list separators), and punctuation.
_AGENT_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# Device prefixes this repo documents. Open roster stays open for custom
# prefixes (Local_Worker); names under these prefixes must match the catalog
# so a typo cannot address work to nobody. Matching is case-insensitive
# (desktop_agent2 → Desktop_Agent2).
_FLEET_AGENT_CATALOG = frozenset(
    {
        "Desktop_Agent1",
        "Laptop_Agent1",
        "server_agent1",
        # Claude Code on the web (task #1556). With the operator's client entry it
        # reaches the hub as a cloud-hosted developer (task #2446); it opens PRs
        # and never merges or deploys.
        "cloud_agent1",
        "cloud_agent13",
        "cloud_agent14",
        "cloud_agent15",
        "cloud_agent16",
        "Desktop_agent3",
        "Laptop_agent3",
        "server_agent3",
        "Desktop_Agent2",
        "Laptop_Agent2",
        "server_agent2",
        "Desktop_Agent4",
        "Laptop_Agent4",
        "server_agent4",
        "Desktop_Agent6",
        "Laptop_Agent6",
        "Desktop_Agent5",
        "Laptop_Agent5",
        "server_agent5",
        # Grok labels: retired from the fleet 2026-09-27 (the operator). Kept in the
        # catalog so any not-yet-revoked token still resolves to its tight
        # research/public profile instead of falling through to developer.
        "Desktop_Agent8",
        "Laptop_Agent8",
        "server_agent8",
        "Desktop_agent12",
        "Desktop_agent10",
        "Desktop_Agent7",
    }
)


def fleet_agent_labels() -> frozenset[str]:
    """The documented agent catalog plus this deployment's operator login label."""
    return _FLEET_AGENT_CATALOG | {operator_label()}


def _fleet_labels_by_lower() -> dict[str, str]:
    return {name.lower(): name for name in fleet_agent_labels()}


def agent_label_is_well_formed(label: str | None) -> bool:
    """True when ``label`` is a non-empty, punctuation-free agent identity."""
    return bool(label) and _AGENT_LABEL_RE.fullmatch(label or "") is not None


# Identity labels that represent a human operator rather than an AI agent
# (task #1217). The operator's login label is the only fleet label minted for
# a human — every catalog entry names an agent surface. The operator's aliases
# (identity_alias_allowlist() values) are the same human under a different
# claimed label and count as human too.
def human_identity_labels() -> frozenset[str]:
    return frozenset({operator_label()})


def _human_alias_values() -> frozenset[str]:
    return frozenset(alias for aliases in identity_alias_allowlist().values() for alias in aliases)


def actor_kind_for_identity(label: str | None) -> str:
    """Classify a resolved identity label as ``"human"``, ``"agent"``, or ``"unknown"``.

    A verified *signature* only proves which credential produced the claim —
    it never by itself proves the signer was a human being. This exists so
    callers (task #1217: evidence_report human_provenance) can keep verified
    *actor* evidence while refusing to promote a verified *agent* identity to
    verified *human authority*.
    """
    if not label:
        return "unknown"
    stripped = label.strip()
    if not stripped:
        return "unknown"
    if stripped in human_identity_labels() or stripped in _human_alias_values():
        return "human"
    lower = stripped.lower()
    canonical = _fleet_labels_by_lower().get(lower)
    if canonical is not None:
        return "human" if canonical in human_identity_labels() else "agent"
    if agent_label_is_well_formed(stripped):
        # Open-roster custom label (e.g. a UA_AGENT_LABELS extension or a
        # bearer-mapped worker not in the documented fleet catalog). It came
        # through the same agent bearer/token infrastructure as fleet agent
        # labels, so treat it as an agent rather than silently granting it
        # human authority by omission.
        return "agent"
    return "unknown"


def is_governed_agent_identity(label: str | None) -> bool:
    """True when ``label`` names a concrete, *governed* agent identity —
    a catalogued fleet label (case-insensitive) or a member of the
    operator's own configured ``UA_AGENT_LABELS`` roster — as opposed to
    merely satisfying the open agent-label syntax.

    ``actor_kind_for_identity``'s open-roster fallback ("any well-formed
    label is an agent") is only safe for identities that arrived through
    the signed producer/bearer infrastructure — there, a well-formed label
    that isn't in the documented fleet catalog is still a real bearer-mapped
    worker. It is NOT safe for an inferred claim that never touched that
    infrastructure: an explicit ``[human: ...]`` annotation or a configured
    operator name (``UA_OPERATOR_NAME``) can be an ordinary human name that
    happens to satisfy the same regex (task #1217 audit finding 1 — e.g.
    ``UA_OPERATOR_NAME=Alice`` must not become ``actor_kind="agent"``).
    Use this predicate (via ``actor_kind_for_claim``) for those sources
    instead of the syntax-only fallback.
    """
    name = (label or "").strip()
    if not name:
        return False
    canonical = _fleet_labels_by_lower().get(name.lower())
    if canonical is not None:
        return canonical not in human_identity_labels()
    roster = agent_labels()
    return bool(roster) and name in roster and name not in _human_alias_values()


def actor_kind_for_claim(label: str | None) -> str:
    """Classify an *inferred* (non-bearer-authenticated) identity claim.

    Sibling of ``actor_kind_for_identity`` for candidate sources that never
    passed through the signed producer/bearer infrastructure — an explicit
    ``[human: ...]`` task-note annotation, a configured operator name, an
    approval ``resolved_by``, or a delegation ``from_agent``. Those sources
    carry no proof the value came through agent tooling, so this never
    falls back to "any well-formed label is an agent": a value is only
    ``"agent"`` when it names a concrete governed identity
    (``is_governed_agent_identity``). An ordinary human name that merely
    matches the label regex (e.g. "Alice") stays ``"unknown"`` here instead
    of becoming a false agent classification (task #1217 audit finding 1),
    while a real fleet label named in a human claim (e.g.
    ``[human: Desktop_Agent2]``) still correctly resolves to ``"agent"`` so
    the caller can surface the contradiction.
    """
    if not label:
        return "unknown"
    stripped = label.strip()
    if not stripped:
        return "unknown"
    if stripped in human_identity_labels() or stripped in _human_alias_values():
        return "human"
    if is_governed_agent_identity(stripped):
        return "agent"
    return "unknown"


def _looks_like_fleet_label(name: str) -> bool:
    lower = name.lower()
    return lower.startswith(("desktop_", "laptop_", "server_"))


def canonical_agent_label(label: str | None) -> str | None:
    """Catalog spelling for a fleet identity, or None if it is not in the catalog."""
    name = (label or "").strip()
    if not name:
        return None
    return _fleet_labels_by_lower().get(name.lower())


def canonical_identity(label: str | None) -> str:
    """The one spelling of an identity label (review of #2315).

    Auth, lifecycle state, its request-path cache and credential revocation
    all key on this, so a case variant the open roster accepts (``desktop_agent2``)
    is the same identity as the catalog label it names. Strips, then maps a
    case-insensitive match to the fleet catalog spelling, else to a registered
    ``UA_AGENT_LABELS`` or human identity spelling; any other label is returned
    stripped. Empty for an empty or non-string value.
    """
    name = label.strip() if isinstance(label, str) else ""
    if not name:
        return ""
    lowered = name.casefold()
    catalog = _fleet_labels_by_lower().get(name.lower())
    if catalog:
        return catalog
    for registered in sorted(agent_labels() | set(human_identity_labels())):
        if registered.casefold() == lowered:
            return registered
    return name


def identity_fold(label: str | None) -> str:
    """Case-folded :func:`canonical_identity`: the comparison key for two labels."""
    return canonical_identity(label).casefold()


def is_human_identity(label: str | None) -> bool:
    """Whether ``label`` is the operator's own identity (its login label or an alias)."""
    folded = identity_fold(label)
    if not folded:
        return False
    return any(identity_fold(h) == folded for h in human_identity_labels() | operator_aliases())


def verified_caller_is_operator() -> bool:
    """Whether the transport-verified caller is the human operator (task #2535).

    The one approval-gate definition of "the human operator made this call":
    :func:`is_human_identity` applied to :func:`verified_agent_identity`. It
    takes no label on purpose, so it can never be applied to a self-claimed
    ``from_agent`` or ``requested_by``. An agent credential cannot reach it by
    being named as an alias either: ``operator_aliases()`` drops any alias that
    names a catalogued or credential-backed worker.
    """
    return is_human_identity(verified_agent_identity())


def agent_labels() -> set[str]:
    """Configured agent labels from ``UA_AGENT_LABELS``.

    Empty when unset: that is the open-roster mode used by a fresh install.
    When the operator sets a roster, ``delegate`` / ``message_send`` refuse
    unknown from/to labels so a typo cannot address a task to nobody.

    Tokens are colon-separated. A comma inside a token is almost always a
    mistaken delimiter (the tokens map uses commas) — warn so a typo like
    ``Desktop_Agent8,server_agent8`` cannot silently collapse into one bogus label.
    """

    raw = os.environ.get("UA_AGENT_LABELS", DEFAULT_AGENT_LABELS).strip()
    if not raw:
        return set()
    labels: set[str] = set()
    for token in raw.split(":"):
        name = token.strip()
        if not name:
            continue
        if "," in name or any(ch.isspace() for ch in name):
            print(
                "ua-audit: [Warning] UA_AGENT_LABELS token "
                f"{name!r} looks malformed (comma or whitespace); "
                "labels are colon-separated — a comma merges two identities "
                "into one bogus label",
                file=sys.stderr,
            )
        elif not agent_label_is_well_formed(name):
            print(
                "ua-audit: [Warning] UA_AGENT_LABELS token "
                f"{name!r} is not a well-formed agent label",
                file=sys.stderr,
            )
        labels.add(name)
    if labels:
        # A closed roster also admits agents an operator added with
        # `unified-agent agent add` (task #2453), so adding one needs no
        # service.env edit or restart. An open roster stays open.
        labels |= _seated_labels()
    return labels


def _seated_labels() -> frozenset[str]:
    """Labels seated by `unified-agent agent add` (``agent_roster``); empty on error."""
    try:
        from . import agent_roster

        return agent_roster.labels()
    except Exception:  # noqa: BLE001 - the env roster stays authoritative on failure
        return frozenset()


def _catalog_key(name: str) -> str:
    """Catalog spelling when ``name`` is a fleet identity, otherwise ``name``."""
    return canonical_agent_label(name) or name


def _excluded_open_roster_identity(name: str) -> bool:
    """Human aliases and expected-unregistered emitters, case-insensitive for catalog names."""
    key = _catalog_key(name)
    if key in human_identity_labels() or name in human_identity_labels():
        return True
    if key in _human_alias_values() or name in _human_alias_values():
        return True
    expected = expected_unregistered_labels()
    return key in expected or name in expected


def is_fleet_roster_label(label: str | None) -> bool:
    """Whether ``label`` is a fleet agent row, not an event-source attribution.

    This is a read filter for ``agent_list`` and host views. It does not close
    the roster and it does not change authorization: ``is_known_agent_label``
    still accepts every well-formed custom label while ``UA_AGENT_LABELS`` is
    unset, and ``DEFAULT_AGENT_LABELS`` stays empty.

    Closed roster (``UA_AGENT_LABELS`` set — the live hub): exact membership in
    that set, the same spelling ``is_known_agent_label`` accepts. A case
    variant such as ``desktop_agent1`` is not a roster member. A custom label
    the operator put in the roster stays a fleet agent.

    Open roster (unset/empty): a ``FLEET_AGENT_LABELS`` catalog identity.
    Custom well-formed labels stay legal for delegate and message_send, and
    become fleet rows only when added to ``UA_AGENT_LABELS`` or bound to a
    registered ua-node (callers OR that set in). That is how ``manual-test``
    and similar attribution strings stay out of fleet counts without refusing
    them as identities.

    On an open roster, human aliases and ``expected_unregistered_labels``
    (cron emitters, the configured operator login) stay out of fleet rows. On a closed
    roster, membership in ``UA_AGENT_LABELS`` wins over that exemption.
    Ill-formed prose never qualifies. Callers must not delete the underlying
    registry row. Peer discovery uses ``is_peer_agent_label``, which keeps
    open-roster custom identities visible without putting them in fleet counts.
    """
    name = (label or "").strip()
    if not name or not agent_label_is_well_formed(name):
        return False
    # An explicit roster entry wins over the drift exemption. The live hub
    # leaves cron emitters and the operator login out of UA_AGENT_LABELS, so they
    # stay attributions there. Putting one in the roster is an operator choice.
    roster = agent_labels()
    if roster:
        return name in roster
    if _excluded_open_roster_identity(name):
        return False
    return canonical_agent_label(name) is not None


def is_peer_agent_label(label: str | None) -> bool:
    """Whether ``label`` may appear in peer discovery (``agent_context``).

    Fleet counts stay on ``is_fleet_roster_label``. Peer discovery is wider
    only while the roster is open: a well-formed custom identity that
    ``is_known_agent_label`` accepts is a real peer (``Local_Worker``), not an
    attribution. Humans, expected-unregistered emitters, and ill-formed prose
    stay out. A closed roster does not grow extra peers beyond its members.
    """
    if is_fleet_roster_label(label):
        return True
    if agent_labels():
        return False
    name = (label or "").strip()
    if not agent_label_is_well_formed(name):
        return False
    if _excluded_open_roster_identity(name):
        return False
    return is_known_agent_label(name)


def is_known_agent_label(label: str | None) -> bool:
    """Whether ``label`` may be used as an agent identity.

    Closed roster (``UA_AGENT_LABELS`` set): membership check.
    Open roster (unset/empty): well-formed custom labels, plus catalog matches
    for Desktop_/Laptop_/server_ names (case-insensitive, so desktop_agent2 is
    Desktop_Agent2). Fleet-prefix names missing from the catalog are refused
    without closing the roster to Local_* (and similar) identities.
    Empty and garbage refs are always refused.
    """
    name = (label or "").strip()
    if not agent_label_is_well_formed(name):
        return False
    roster = agent_labels()
    if roster:
        return name in roster
    if _looks_like_fleet_label(name):
        # An operator-added fleet-prefixed label is known too (task #2453).
        return name.lower() in _fleet_labels_by_lower() or name in _seated_labels()
    return True


def unknown_agent_label_error(field: str, label: str) -> str:
    """ToolError / ValueError text for a refused agent identity."""
    roster = agent_labels()
    if roster:
        return (
            f"{field} {label!r} is not a known agent label; known labels: "
            f"{', '.join(sorted(roster))} (extend via UA_AGENT_LABELS)"
        )
    name = (label or "").strip()
    if _looks_like_fleet_label(name) and name.lower() not in _fleet_labels_by_lower():
        return (
            f"{field} {label!r} is not a governed fleet identity "
            "(open roster accepts custom prefixes; Desktop_/Laptop_/server_ "
            "names must match the catalog)"
        )
    return f"{field} {label!r} is not a valid agent label"


# Non-developer hub citizens (task #772). They may claim, complete, and
# attribute work, but never mint a git worktree. Compiled, not env: this is a
# property of those identities, not of a host roster. Do not add them to
# dispatch CLI families without an explicit go.
NO_WORKTREE_AGENT_LABELS = frozenset({"Desktop_Agent8", "Laptop_Agent8"})


def agent_skips_task_worktree(label: str | None) -> bool:
    """True when this identity must not create a task git worktree."""
    return (label or "").strip() in NO_WORKTREE_AGENT_LABELS


# Task queue lanes (UA Task Queue V2.0 increment 1, task #705; actor model, #883).
# `lane` answers *who acts on this task*, not what it is about — the portfolio
# bucket (personal / professional / hobby) lives on `domain` alone now, and the
# 2026-09 migration remaps those legacy lane values to 'default'. Actor lanes:
#   default        — untriaged shared pool; any agent may claim
#   dispatch-ready  — triaged + vetted; the pool review passes and the dispatch
#                     scheduler work from. Claimable like 'default'.
#   operator-only   — a human step only the operator can do; never auto-dispatched
#   interactive-only — an operator-started task; explicit lane claim required,
#                      never part of the shared pickup or dispatch pool
#   tracking        — an umbrella / roadmap index, not a unit of work; unclaimable
#   laptop          — host-routed work for the laptop dev checkout
# Registered agent labels are also valid lanes for delegation-targeted work.
TASK_SYSTEM_LANES = (
    "default",
    "dispatch-ready",
    "operator-only",
    "interactive-only",
    "tracking",
    "laptop",
)

# Lanes an unscoped claim (no expected_lane) or the dispatch scheduler may pull
# from — the open shared pool. Other system lanes need an explicit lane match.
TASK_OPEN_POOL_LANES = ("default", "dispatch-ready")

# Lanes that never represent claimable work.
TASK_UNCLAIMABLE_LANES = ("tracking",)

# Legacy lane values remapped to 'default' by the 2026-09 actor-model migration.
TASK_LEGACY_LANES = ("personal", "professional", "hobby")


# One-release compatibility for stored actor lanes and published API keys.
# These explicit legacy spellings remain visible to the privacy inventory.
LEGACY_OPERATOR_LANE = "legacy-operator-only"
LEGACY_OPERATOR_QUEUE_KEY = "legacy_operator_only"
LEGACY_OPERATOR_ATTENTION_KIND = "awaiting_legacy_operator"
LEGACY_OPERATOR_DIGEST_KEY = "waiting_on_legacy_operator"


def canonical_task_lane(lane: str | None) -> str:
    """Normalize the retired operator lane without changing identity authority."""
    value = (lane or "").strip()
    return "operator-only" if value == LEGACY_OPERATOR_LANE else value


def task_lane_values() -> tuple[str, ...]:
    """Allowed lane slugs stored on tasks.lane (system + registered agents)."""
    return TASK_SYSTEM_LANES + tuple(sorted(agent_labels()))


def is_valid_task_lane(lane: str | None) -> bool:
    """True for a system lane, a configured agent lane, or an open-roster known label."""
    name = canonical_task_lane(lane)
    if not name:
        return False
    if name in TASK_SYSTEM_LANES:
        return True
    return is_known_agent_label(name)


def task_lane_sql_check() -> str:
    """SQLite CHECK clause for tasks.lane (format only; values validated in Python)."""
    return "CHECK (length(lane) >= 1 AND length(lane) <= 64)"


LIVENESS_SWEEP_MODES = ("off", "report", "enforce")


def liveness_sweep_mode() -> str:
    """``UA_LIVENESS_SWEEP``: off | report | enforce (default report, task #1749).

    ``report`` records a digest entry per task with no action path and moves
    nothing, so deploying the sweep never mass-moves live tasks. ``enforce``
    also moves such a task to ``blocked``. An unknown value falls back to
    ``report`` with a warning rather than to ``enforce``.
    """
    raw = (os.environ.get("UA_LIVENESS_SWEEP") or "").strip().lower()
    if not raw:
        return "report"
    if raw in LIVENESS_SWEEP_MODES:
        return raw
    _warn_posture_once(
        "liveness_sweep",
        f"ua-audit: [Warning] UA_LIVENESS_SWEEP {raw!r} is not one of "
        f"{LIVENESS_SWEEP_MODES}; using 'report'",
    )
    return "report"


def agent_wip_max() -> int:
    """Max concurrent `doing` tasks per agent (expired locks excluded).

    ``UA_AGENT_WIP_MAX``, default 20. The default was 2 until 2026-11-22,
    while production ran 20: the documented number and the enforced number had
    drifted apart, and every queue note quoting "refused at 2" described a cap
    the fleet never ran. 20 is now the documented default too (task #2759).
    ``agent_wip_max_for()`` layers ``UA_AGENT_WIP_OVERRIDES`` on top.
    """
    raw = os.environ.get("UA_AGENT_WIP_MAX", "20").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 20


def agent_wip_overrides() -> dict[str, int]:
    """Per-agent WIP caps from ``UA_AGENT_WIP_OVERRIDES`` (``label:max`` entries).

    Comma-separated, whitespace tolerant; the floor is 1. Labels go through
    :func:`canonical_identity`, the spelling the claim path stores in
    ``claimed_by``. A malformed entry (no colon, non-integer, empty label, or a
    label already listed) is ignored with one posture warning, never an error.
    """
    raw = os.environ.get("UA_AGENT_WIP_OVERRIDES", "")
    overrides: dict[str, int] = {}
    bad: list[str] = []
    for entry in raw.split(","):
        part = entry.strip()
        if not part:
            continue
        label_s, sep, max_s = part.rpartition(":")
        label = canonical_identity(label_s) if sep else ""
        try:
            cap = max(1, int(max_s.strip()))
        except ValueError:
            cap = 0
        if not label or not cap or label in overrides:
            bad.append(part)
            continue
        overrides[label] = cap
    if bad:
        _warn_posture_once(
            "agent_wip_overrides",
            "ua-audit: [Warning] UA_AGENT_WIP_OVERRIDES ignored malformed or "
            f"duplicate entries {bad!r}; expected comma-separated label:max",
        )
    return overrides


def agent_wip_max_for(agent: str | None) -> int:
    """Per-agent WIP cap: the ``UA_AGENT_WIP_OVERRIDES`` value for ``agent``, else the default."""
    label = canonical_identity(agent)
    if label:
        override = agent_wip_overrides().get(label)
        if override is not None:
            return override
    return agent_wip_max()


def fleet_wip_max() -> int:
    """Fleet-wide soft WIP cap on direct ``task_claim`` (task #1838).

    ``UA_FLEET_WIP_MAX``; unset defaults to 40. The two gates count different
    ledgers: this one counts live claimed ``doing`` tasks, the dispatcher's
    ``dispatch_max_live_runs()`` counts ``dispatch_runs`` launch slots. There
    is no shared slot pool. The soft cap must sit *above* the per-agent cap
    (``agent_wip_max()``, default 20): when both defaults were 20 the fleet
    warning fired at the same claim as the per-agent refusal and carried no
    information (task #2759). A claim that would take the fleet past this cap
    still succeeds and returns a ``wip_warnings`` entry. Non-integer values
    fall back to the default; the floor is 1, so a misconfigured cap never
    means "no cap".
    """
    default = 40
    raw = os.environ.get("UA_FLEET_WIP_MAX", "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def fleet_wip_hard_max() -> int:
    """Fleet-wide hard WIP ceiling on direct ``task_claim`` (task #1838).

    ``UA_FLEET_WIP_HARD_MAX``; unset or non-integer defaults to twice
    ``fleet_wip_max()`` (80 with the default soft cap of 40, task #2759). A
    value below the soft cap is raised to it. A new claim is refused once this
    many claimed ``doing`` tasks are live, except for high-priority or
    ``[urgency: now]`` work (warned, never refused).
    """
    soft = fleet_wip_max()
    raw = os.environ.get("UA_FLEET_WIP_HARD_MAX", "").strip()
    try:
        hard = int(raw) if raw else 2 * soft
    except ValueError:
        hard = 2 * soft
    return max(soft, hard)


# Lanes the fleet WIP cap never warns on or refuses (task #1838): a human step
# and operator-started work keep their claim semantics unchanged. Their live
# claims still count toward the fleet total.
FLEET_WIP_EXEMPT_LANES = ("operator-only", "interactive-only")

# How long a ``doing`` row with no claimant keeps counting as live WIP (task
# #1886). Such a row reached ``doing`` through ``task_update`` and holds no
# lease, so it is treated as if it held a default-length ``task_claim`` lease
# (one hour) from its last write: any update to the row renews it, and past it
# the row stops occupying a slot, like an expired lease. ``queue_pressure``
# still reports it as ``unowned_doing`` either way.
TASK_CLAIM_DEFAULT_LEASE_SECONDS = 3600
UNCLAIMED_DOING_LEASE_SECONDS = TASK_CLAIM_DEFAULT_LEASE_SECONDS


def lane_wip_limits() -> dict[str, dict[str, str | int]]:
    """Per-lane WIP caps from UA_LANE_WIP (lane:max:mode comma-separated).

    mode is ``soft`` (warn, still claim) or ``hard`` (refuse at task_claim).
    Unlisted lanes have no per-lane cap — only the per-agent cap applies.
    """
    raw = os.environ.get("UA_LANE_WIP", "").strip()
    limits: dict[str, dict[str, str | int]] = {}
    if not raw:
        return limits
    for entry in raw.split(","):
        part = entry.strip()
        if not part:
            continue
        pieces = part.split(":")
        if len(pieces) != 3:
            continue
        lane, max_s, mode = pieces[0].strip(), pieces[1].strip(), pieces[2].strip().lower()
        lane = canonical_task_lane(lane)
        if not is_valid_task_lane(lane):
            continue
        try:
            wip_max = max(1, int(max_s))
        except ValueError:
            continue
        if mode not in ("soft", "hard"):
            mode = "soft"
        limits[lane] = {"wip_max": wip_max, "mode": mode}
    return limits


# Identities this codebase itself emits and will never register. Kept as a code
# default rather than left to deployment config because they are a property of
# the source, not of any host: `monitor.py` stamps `unified-agent-cron` on every
# scheduled run, and `weekly-export-cron.sh` stamps `weekly-export-cron` (plus
# an optional `weekly-export-manual` author for hand runs), so a deployment that
# "forgot" to exempt them would not be configured differently — it would be
# reporting a known fact as drift forever. These identities are emitted by
# supported source/runtime paths but are not delegation labels. Keeping them
# here makes the exception explicit and keeps the identity check authoritative
# for genuinely unknown labels.
DEFAULT_EXPECTED_UNREGISTERED_LABELS = "unified-agent-cron:weekly-export-cron:weekly-export-manual"

# Labels whose silence is expected by topology rather than symptomatic. The
# default is deliberately EMPTY: under the single-hub topology (laptop agents
# reach the desktop hub directly over Tailscale, 2026-08-20) a laptop agent
# heartbeats like any other, so its absence is a real signal and suppressing it
# would hide the outage this view exists to surface. The operator consolidated to one
# hub precisely because two hubs split context, tools, memory, and history.
#
# The seam remains for a tier that genuinely cannot report — set
# UA_EXPECTED_OFFLINE_AGENT_LABELS (colon-separated) to opt a label out. Doing so
# is a deliberate reduction in alerting, not a noise fix; record why.
DEFAULT_EXPECTED_OFFLINE_AGENT_LABELS = ""


def expected_unregistered_labels() -> set[str]:
    """Labels allowed to be active without appearing in UA_AGENT_LABELS.

    `agent_label_drift` exists to surface identities nobody declared. Some are
    declared-by-decision to stay unregistered — the operator's human web-UI
    identity (``operator_label()``) holds a bearer token and is deliberately absent from the
    registry (memory `92437723a6e8`) — and without an exemption the check reads
    DORMANT permanently, which makes a real unknown identity indistinguishable
    from the two expected ones.

    NOTE the semantics differ from `UA_AUDIT_ROOTS`, deliberately.
    `UA_EXPECTED_UNREGISTERED_LABELS` is UNION-with-default, not replace: the
    default names identities this repository's own code emits, and dropping
    them by writing an unrelated entry into the variable would be a silent
    regression of exactly the kind replace-semantics caused for the audit roots
    (see the drop-in at unified-agent.service.d/audit-roots.conf). To stop
    exempting a shipped identity, change the default here.
    """
    raw = os.environ.get("UA_EXPECTED_UNREGISTERED_LABELS", "").strip()
    configured = {x.strip() for x in raw.split(":") if x.strip()}
    shipped = {x.strip() for x in DEFAULT_EXPECTED_UNREGISTERED_LABELS.split(":")}
    return configured | shipped | {operator_label()}


def expected_offline_agent_labels() -> set[str]:
    """Configured labels whose offline state is expected by topology."""
    raw = os.environ.get("UA_EXPECTED_OFFLINE_AGENT_LABELS", "").strip()
    configured = {x.strip() for x in raw.split(":") if x.strip()}
    return configured | {
        x.strip() for x in DEFAULT_EXPECTED_OFFLINE_AGENT_LABELS.split(":") if x.strip()
    }


# Baseline is delegate only. pi_run_task is force-gated inside that tool;
# git_merge_task and node_dispatch already pass force=True through
# check_prod_action_gate once a verified identity is present.
_APPROVAL_BASELINE = frozenset({"delegate"})


def approval_baseline_enabled() -> bool:
    """Whether the default approval baseline is unioned into the gated set.

    On unless ``UA_APPROVAL_BASELINE`` is ``0``/``false``/``no``, or
    ``UA_LEGACY_OPEN_POSTURE`` is set to ``1``/``true``/``yes``.
    """
    if legacy_open_posture():
        return False
    token = _env_token("UA_APPROVAL_BASELINE")
    if token is None:
        return True
    return token not in _ENV_OFF


def effective_approval_tools(
    tools_raw: str | None,
    baseline_raw: str | None,
    legacy_raw: str | None,
) -> set[str]:
    """Gated tool names for an explicit settings triple. Does not read env or warn.

    A blank ``baseline_raw`` leaves the baseline on. ``legacy_raw`` of
    ``1``/``true``/``yes`` suppresses it. Explicit tool names are always kept.
    """
    tools = {part.strip() for part in (tools_raw or "").split(":") if part.strip()}
    legacy_on = (legacy_raw or "").strip().lower() in _ENV_ON
    baseline_token = (baseline_raw or "").strip().lower()
    if not legacy_on and baseline_token not in _ENV_OFF:
        tools |= set(_APPROVAL_BASELINE)
    return tools


def approval_gated_tools(raw: str | None = None) -> set[str]:
    """Tool names gated by the global approval queue (task 62 · C).

    Colon-separated ``UA_APPROVAL_TOOLS`` in the hub's server config, unioned
    with the baseline ``{"delegate"}`` unless ``UA_APPROVAL_BASELINE`` is
    ``0``/``false``/``no`` or ``UA_LEGACY_OPEN_POSTURE=1``. Explicit entries
    are always included. This is a hub-side global allowlist (the operator's
    2026-07-17 decision), the same layer as the run_cmd allowlist: agents
    cannot opt themselves in or out.

    ``UA_APPROVAL_BASELINE=0`` restores the old empty opt-in and warns once.
    Callers that already resolved another process's settings (system_doctor)
    use ``effective_approval_tools`` so this process's env is not substituted.
    """
    tools_raw = raw if raw is not None else os.environ.get("UA_APPROVAL_TOOLS", "")
    tools = effective_approval_tools(
        tools_raw,
        os.environ.get("UA_APPROVAL_BASELINE"),
        os.environ.get("UA_LEGACY_OPEN_POSTURE"),
    )
    if approval_baseline_enabled():
        return tools
    if _env_off("UA_APPROVAL_BASELINE"):
        _warn_posture_once(
            "approval-baseline-off",
            "ua-audit: [Warning] UA_APPROVAL_BASELINE=0: the approval baseline "
            "is off, so delegate is not gated unless listed in "
            "UA_APPROVAL_TOOLS. Unset UA_APPROVAL_BASELINE to restore the "
            "baseline.",
        )
    elif legacy_open_posture():
        _warn_posture_once(
            "legacy-open-approvals",
            "ua-audit: [Warning] UA_LEGACY_OPEN_POSTURE=1: the approval baseline "
            "is off, so delegate is not gated unless listed in "
            "UA_APPROVAL_TOOLS. Unset UA_LEGACY_OPEN_POSTURE to restore the "
            "baseline.",
        )
    return tools


def elicitation_approvals_enabled() -> bool:
    """Whether a gated row may be resolved via MCP elicitation (task #829). OPT-IN.

    ``UA_ELICITATION_APPROVALS=1`` turns on the in-client approval form. Unset
    (the default) leaves elicitation dormant: a gated call still parks its row
    and raises, and only the ``/ui`` queue resolves it. Enabling this is the
    operator asserting that elicitation on this hub reaches a human — the flag,
    plus the caller's profile still having to permit ``approval_resolve``, plus
    a server-issued one-time challenge on round two (task #832), is what stops
    an autonomous agent advertising elicitation support and approving its own
    pending request.
    """
    return os.environ.get("UA_ELICITATION_APPROVALS", "") == "1"


APPROVAL_PASS_TTL_HOURS_DEFAULT = 24.0


def approval_pass_ttl_hours() -> float:
    """How long an approved-but-unconsumed one-shot pass stays valid (task #171).

    An approval is a time-bounded human decision about a specific action. Without
    a TTL an unconsumed pass is a permanent grant: two passes approved
    2026-07-19 were still live a week later, inert only because UA_APPROVAL_TOOLS
    happened to be empty. That contradicts the standing rule that approvals are
    not durable.

    A non-positive or unparseable UA_APPROVAL_PASS_TTL_HOURS falls back to the
    default rather than disabling expiry — a misconfigured value must not turn
    passes permanent again.
    """
    try:
        val = float(os.environ.get("UA_APPROVAL_PASS_TTL_HOURS", ""))
    except ValueError:
        return APPROVAL_PASS_TTL_HOURS_DEFAULT
    return val if val > 0 else APPROVAL_PASS_TTL_HOURS_DEFAULT


# --- swarm event bus (task #157) -----------------------------------------------

DEFAULT_EVENT_SOCKET = Path("~/.config/agent_hub/events.sock")

# An append-only ledger that trims itself is one a consumer cannot reason about,
# so this is never applied automatically — events.prune_events() is an explicit
# operator action. 0 means keep forever.
DEFAULT_EVENT_LOG_RETENTION_DAYS = 30.0


def event_socket_path() -> Path:
    """Where the local pub/sub bus listens (UA_EVENT_SOCKET)."""
    return Path(os.environ.get("UA_EVENT_SOCKET", str(DEFAULT_EVENT_SOCKET))).expanduser()


def lifecycle_capture_enabled() -> bool:
    """Optional sanitized lifecycle capture (task #482). Default off."""
    return os.environ.get("UA_LIFECYCLE_CAPTURE", "0") == "1"


def lifecycle_exclude_kinds() -> set[str]:
    """Colon-separated lifecycle kinds that must not be recorded."""
    raw = os.environ.get("UA_LIFECYCLE_EXCLUDE_KINDS", "")
    return {item.strip() for item in raw.split(":") if item.strip()}


def lifecycle_exclude_tools() -> set[str]:
    """Colon-separated tool names whose start/end observations are dropped.

    Defaults skip tools that commonly carry prompts or secrets in arguments.
    Set UA_LIFECYCLE_EXCLUDE_TOOLS="" to exclude nothing extra.
    """
    raw = os.environ.get("UA_LIFECYCLE_EXCLUDE_TOOLS", "gemini_ask:pi_run_task")
    return {item.strip() for item in raw.split(":") if item.strip()}


def event_bus_enabled() -> bool:
    """Whether mutations mirror onto the bus (UA_EVENT_BUS=0 to disable).

    Disabling turns off the implicit mirror from ``record_agent_event`` and the
    socket fan-out. It deliberately does NOT disable ``events.record_event``
    itself: a caller that explicitly writes to the ledger — task #156's policy
    engine, which fails closed on a missing record — must not be silently
    disarmed by an operator turning off push notifications.
    """
    return os.environ.get("UA_EVENT_BUS", "1") != "0"


def event_supervision_enabled() -> bool:
    """Whether task status transitions emit FirstMate-style supervisor wakes.

    ``UA_EVENT_SUPERVISION=0`` disables ``agent.supervisor.wake`` emission
    (task #1028). Default on — zero-token classification still runs at the
    mutation site; only the durable wake channel is gated.
    """
    return os.environ.get("UA_EVENT_SUPERVISION", "1") != "0"


def message_max_hops() -> int:
    """Max reply-chain depth for typed inter-agent messages (#483, #2387). Default 16."""
    raw = os.environ.get("UA_MESSAGE_MAX_HOPS", "16")
    try:
        val = int(raw)
    except ValueError:
        return 16
    return val if val >= 1 else 16


def message_pair_streak_max() -> int:
    """Short messages in a row one agent pair may exchange (task #2387). Default 4; 0 is off."""
    try:
        val = int(os.environ.get("UA_MESSAGE_PAIR_STREAK_MAX", "4"))
    except ValueError:
        return 4
    return val if val >= 0 else 4


def message_max_recipients() -> int:
    """Recipients one office or role send may fan out to (task #2392). Default 8.

    A value below 1, or one that is not an integer, falls back to 8. The cap is
    the number of messages the send would write, after the sender is left out.
    """
    try:
        val = int(os.environ.get("UA_MESSAGE_MAX_RECIPIENTS", "8"))
    except ValueError:
        return 8
    return val if val >= 1 else 8


def message_max_body_bytes() -> int:
    """Max UTF-8 body size for typed messages (task #483). Default 4096."""
    raw = os.environ.get("UA_MESSAGE_MAX_BYTES", "4096")
    try:
        val = int(raw)
    except ValueError:
        return 4096
    return val if val >= 64 else 4096


# Messaging bridges (task #1863): unified_brain threads and Hermes A2A gateways
# <-> the hub mailbox. Run by `unified-agent-bridge run-once`; no timer ships.


def bridge_brain_repo() -> Path:
    """Checkout of the unified_brain thread repo the bridge reads and appends to.

    ``UA_BRIDGE_BRAIN_REPO``, else ``unified_brain`` under ``UA_DEV_ROOT``.
    """
    raw = os.environ.get("UA_BRIDGE_BRAIN_REPO", "").strip()
    return Path(raw).expanduser() if raw else dev_root() / "unified_brain"


def bridge_state_path() -> Path:
    """The bridge's cursor and idempotency file (``UA_BRIDGE_STATE``, default in UA_HOME)."""
    raw = os.environ.get("UA_BRIDGE_STATE", "").strip()
    return Path(raw).expanduser() if raw else data_home() / "bridge-state.json"


def bridge_sender() -> str:
    """Hub label the brain bridge sends relayed thread entries from.

    ``UA_BRIDGE_SENDER``, default ``brain_bridge``. Thread headings are not
    authenticated, so relayed mail never carries the author a thread names; it
    comes from this one service label, and the claimed author goes in the
    untrusted header. With a closed roster (``UA_AGENT_LABELS``) the label must
    be listed there. It needs no token: the bridge sends in process.
    """
    return os.environ.get("UA_BRIDGE_SENDER", "").strip() or "brain_bridge"


def bridge_brain_relay_groups() -> frozenset[str]:
    """unified_brain roster groups whose thread entries the bridge relays to the hub.

    ``UA_BRIDGE_BRAIN_FROM_GROUPS`` (comma-separated), default ``cloud``: agents
    without hub access. Local agents already have the hub, so their entries are
    never relayed back, which is also what keeps the two directions from looping.
    """
    raw = os.environ.get("UA_BRIDGE_BRAIN_FROM_GROUPS", "cloud")
    groups = frozenset(g.strip() for g in raw.split(",") if g.strip())
    return groups or frozenset({"cloud"})


def bridge_brain_group_recipient() -> str | None:
    """Hub label that receives entries addressed only to a group (``local``, ``any``).

    ``UA_BRIDGE_BRAIN_GROUP_TO``; unset (the default) leaves group-only threads
    to the session-start inbox read, so the bridge never fans one entry out.
    """
    raw = os.environ.get("UA_BRIDGE_BRAIN_GROUP_TO", "").strip()
    return raw or None


def bridge_max_text_bytes() -> int:
    """Cap on relayed text, in UTF-8 bytes (``UA_BRIDGE_MAX_BYTES``, default 3000).

    A relayed hub message is the untrusted header plus this text, and it must fit
    ``UA_MESSAGE_MAX_BYTES``: ``messaging.bridge_state.fit_body`` lowers the text
    budget to what is left after the header, so this is an upper bound only.
    """
    raw = os.environ.get("UA_BRIDGE_MAX_BYTES", "3000")
    try:
        val = int(raw)
    except ValueError:
        return 3000
    return val if val >= 64 else 3000


def bridge_a2a_enabled() -> bool:
    """Forward hub mail to Hermes A2A gateways (``UA_BRIDGE_A2A=1``). Off by default."""
    return _env_on("UA_BRIDGE_A2A")


def bridge_a2a_peers() -> dict[str, str]:
    """Hub label -> A2A JSON-RPC endpoint (``UA_BRIDGE_A2A_PEERS="Label=url,..."``).

    Holds no credential. The forwarder calls only loopback endpoints, which a
    Hermes gateway with no token configured accepts (its localhost-only mode).
    """
    peers: dict[str, str] = {}
    for item in os.environ.get("UA_BRIDGE_A2A_PEERS", "").split(","):
        label, sep, url = item.partition("=")
        if sep and label.strip() and url.strip():
            peers[label.strip()] = url.strip()
    return peers


def bridge_a2a_timeout_s() -> float:
    """Seconds to wait for a gateway's reply (``UA_BRIDGE_A2A_TIMEOUT``, default 330).

    Hermes holds a ``message/send`` open until the live session answers or its own
    300-second reply timeout passes, so the default waits a little longer.
    """
    try:
        val = float(os.environ.get("UA_BRIDGE_A2A_TIMEOUT", "330"))
    except ValueError:
        return 330.0
    return val if val > 0 else 330.0


def event_log_retention_days() -> float:
    """Retention window for events.prune_events(). Misconfiguration keeps the
    default rather than silently deleting the whole log or nothing at all."""
    raw = os.environ.get("UA_EVENT_LOG_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_EVENT_LOG_RETENTION_DAYS
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_EVENT_LOG_RETENTION_DAYS
    return val if val >= 0 else DEFAULT_EVENT_LOG_RETENTION_DAYS


# --- gateway read coalescing (task #1922) --------------------------------------
# Read-only ALLOW decisions were >90% of event_log on the hub (557k
# agent.gateway.decision rows in 7 days). They are counted in memory and written
# as one agent.gateway.decision.rollup event per key and bucket; see
# gateway._ReadCoalescer. The bucket is also the most a crash can lose.
DEFAULT_GATEWAY_COALESCE_BUCKET_S = 3600
_GATEWAY_COALESCE_BUCKET_BOUNDS = (60, 86400)


def gateway_coalesce_reads() -> bool:
    """``UA_GATEWAY_COALESCE_READS``: on unless set to 0/false/no/off."""
    return not env_flag_off("UA_GATEWAY_COALESCE_READS")


def gateway_coalesce_bucket_s() -> int:
    """``UA_GATEWAY_COALESCE_BUCKET_S``, 60..86400 seconds; anything else is 3600."""
    raw = os.environ.get("UA_GATEWAY_COALESCE_BUCKET_S")
    if raw is None:
        return DEFAULT_GATEWAY_COALESCE_BUCKET_S
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_GATEWAY_COALESCE_BUCKET_S
    low, high = _GATEWAY_COALESCE_BUCKET_BOUNDS
    return value if low <= value <= high else DEFAULT_GATEWAY_COALESCE_BUCKET_S


# --- agent perf samples (task #919) -------------------------------------------
# monitor.instrumented() appends one agent_perf_samples row per instrumented
# tool call. On a busy hub this is the single fastest-growing table (#919:
# 3.4M rows / ~422 MB with indexes, ~89% of memory.db, no prior retention).
# Every reader (monitor.perf_summary, scripts/gate_replay, the webui perf
# endpoints) filters to a window of at most 24h, so a short retention window
# loses nothing observable. backup_runner prunes on the daily backup cadence.
DEFAULT_PERF_SAMPLE_RETENTION_DAYS = 14.0


def perf_sample_retention_days() -> float:
    """Retention window for storage.prune_perf_samples() (UA_PERF_SAMPLE_RETENTION_DAYS).

    Misconfiguration keeps the default rather than silently deleting everything
    or nothing. ``0`` means keep forever.
    """
    raw = os.environ.get("UA_PERF_SAMPLE_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_PERF_SAMPLE_RETENTION_DAYS
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_PERF_SAMPLE_RETENTION_DAYS
    return val if val >= 0 else DEFAULT_PERF_SAMPLE_RETENTION_DAYS


# --- dispatch-run settings files (task #2191) ----------------------------------
# Each opted-in Claude dispatch writes a per-run settings file and sidecar under
# data_home()/dispatch-runs. The run's end removes them; queue hygiene expires
# any left behind (a killed supervisor, a restart) once older than this window.
# Launch logs in the same directory are evidence and are never expired by it.
DEFAULT_DISPATCH_SETTINGS_RETENTION_DAYS = 7.0


def dispatch_settings_retention_days() -> float:
    """Age past which stale dispatch settings/sidecar files are deleted
    (UA_DISPATCH_SETTINGS_RETENTION_DAYS, default 7).

    ``0`` keeps them forever. A malformed, negative or non-finite value keeps
    the default rather than deleting everything or nothing.
    """
    raw = os.environ.get("UA_DISPATCH_SETTINGS_RETENTION_DAYS")
    if raw is None or not raw.strip():
        return DEFAULT_DISPATCH_SETTINGS_RETENTION_DAYS
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_DISPATCH_SETTINGS_RETENTION_DAYS
    if not math.isfinite(val) or val < 0:
        return DEFAULT_DISPATCH_SETTINGS_RETENTION_DAYS
    return val


def load_gemini_env() -> None:
    if "GEMINI_API_KEY" not in os.environ:
        env_file = Path("~/.config/unified-agent/gemini.env").expanduser()
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    os.environ["GEMINI_API_KEY"] = line.partition("=")[2].strip()


def ensure_private_directory(path: Path) -> None:
    """Create ``path`` at mode 0700. Leave an existing directory unchanged.

    ``Path.mkdir`` applies ``mode`` after the process umask, so a directory
    this call creates is chmod'd to 0700. Ancestors that already exist are
    not changed: opening the store refuses a group- or world-writable parent
    instead of tightening a directory the operator chose.
    """
    if path.is_dir():
        return
    parent = path.parent
    if parent != path:
        ensure_private_directory(parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
        return
    os.chmod(path, 0o700)


def ensure_dirs() -> None:
    if os.environ.get("UA_SYNTHETIC_WORKFLOW") == "1":
        # The explicit disposable home already exists. Acceptance must neither
        # load a private credential file nor create an inherited workspace.
        return
    load_gemini_env()
    # Hub-owned directories, not caller-scoped fs access: a DENY identity must
    # not make storage.init_db() raise (task #1317). The data home is created
    # first: the default workspace lives inside it, and mkdir(parents=True)
    # would otherwise create that parent at the umask mode.
    ensure_private_directory(data_home())
    process_workspace_root().mkdir(parents=True, exist_ok=True)


def dash_docset_paths() -> list[Path]:
    """Get paths to search for Dash docsets.

    Includes custom paths from UA_DASH_DOCSETS (colon-separated list of directories)
    and default macOS and Zeal paths.
    """
    paths = []
    raw_env = os.environ.get("UA_DASH_DOCSETS")
    if raw_env:
        for p in raw_env.split(":"):
            if p:
                paths.append(Path(p).expanduser())

    home = Path.home()
    default_paths = [
        home / "Library/Application Support/Dash/DocSets",
        home / "Library/Application Support/Zeal/Zeal/docsets",
        home / ".local/share/Zeal/Zeal/docsets",
    ]
    for p in default_paths:
        paths.append(p)

    return [p.resolve() for p in paths if p.exists()]


def untrusted_agents() -> set[str]:
    """Parse UA_UNTRUSTED_AGENTS from the environment into a set of agent labels.

    Supports a comma-separated list of agent labels.
    """
    raw = os.environ.get("UA_UNTRUSTED_AGENTS", "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_agent_untrusted(agent_label: str | None) -> bool:
    """Check if the given agent label is marked as untrusted."""
    if not agent_label:
        return False
    return agent_label in untrusted_agents()


def edition() -> str:
    """Return active edition: 'core' or 'enterprise' (default, task #782)."""
    from . import edition as ed_module

    return ed_module.current_edition()


def is_enterprise_edition() -> bool:
    """True if running in enterprise/evidence edition (default)."""
    return edition() == "enterprise"


# --- evidence chain-head anchor backends (task #1265, F2) --------------------
# Today's default witness (task #768) is a dated note in the operator's own
# vault: an external record, but not independent of the operator. These
# settings select and configure the pluggable backends in
# ``audit/anchor.py`` that anchor the chain head to something the operator
# does not solely control (an RFC-3161 TSA, or a separately configured git
# repo). ``vault`` stays the default so nothing regresses.
DEFAULT_EVIDENCE_ANCHOR_BACKEND = "vault"
DEFAULT_EVIDENCE_TSA_URL = "https://freetsa.org/tsr"
DEFAULT_EVIDENCE_TSA_TIMEOUT_S = 10.0
DEFAULT_EVIDENCE_ANCHOR_GIT_DIR = "evidence-anchors"


def evidence_anchor_backend() -> str:
    """Which :mod:`audit.anchor` backend ``re-anchor``/report-header lookups use.

    ``UA_EVIDENCE_ANCHOR_BACKEND``: one of ``vault`` (default, legacy),
    ``tsa``, or ``git``. Unrecognized values are validated by
    ``audit.anchor.get_backend`` at call time, not here.
    """
    raw = os.environ.get("UA_EVIDENCE_ANCHOR_BACKEND", "").strip().lower()
    return raw or DEFAULT_EVIDENCE_ANCHOR_BACKEND


def evidence_tsa_url() -> str:
    """RFC-3161 TSA endpoint for the ``tsa`` anchor backend.

    ``UA_EVIDENCE_TSA_URL``. Defaults to freetsa.org's public timestamping
    service (fine for a pilot) but is always overridable — this is never the
    only TSA the backend can talk to.
    """
    raw = os.environ.get("UA_EVIDENCE_TSA_URL", "").strip()
    return raw or DEFAULT_EVIDENCE_TSA_URL


def evidence_tsa_cert_path() -> Path | None:
    """Path to a PEM containing the pinned TSA signer certificate used to *verify*
    anchors. ``UA_EVIDENCE_TSA_CERT_PATH``. Never used to select what to
    trust for anchoring itself — only for verification, and verification
    trusts exactly the signer certificate in this file, never a certificate
    embedded in the token. A CA-only PEM does not verify a leaf-signed token."""
    raw = os.environ.get("UA_EVIDENCE_TSA_CERT_PATH", "").strip()
    return Path(raw).expanduser() if raw else None


def evidence_tsa_timeout_s() -> float:
    """Request timeout (seconds) for the TSA HTTP round trip. ``UA_EVIDENCE_TSA_TIMEOUT_S``."""
    raw = os.environ.get("UA_EVIDENCE_TSA_TIMEOUT_S")
    if not raw:
        return DEFAULT_EVIDENCE_TSA_TIMEOUT_S
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_EVIDENCE_TSA_TIMEOUT_S
    except ValueError:
        return DEFAULT_EVIDENCE_TSA_TIMEOUT_S


def evidence_anchor_git_repo() -> Path | None:
    """Path to the git repo the ``git`` anchor backend commits into.

    ``UA_EVIDENCE_ANCHOR_GIT_REPO``. Must already exist and be initialized —
    the backend never runs ``git init``. Kept separate from the workspace
    repo on purpose: a witness that lives in the same repo an operator can
    already rewrite is not independent of them.
    """
    raw = os.environ.get("UA_EVIDENCE_ANCHOR_GIT_REPO", "").strip()
    return Path(raw).expanduser() if raw else None


def evidence_anchor_git_dir() -> str:
    """Relative directory (inside the anchor repo) anchor files are committed under.

    ``UA_EVIDENCE_ANCHOR_GIT_DIR`` (default ``evidence-anchors``)."""
    raw = os.environ.get("UA_EVIDENCE_ANCHOR_GIT_DIR", "").strip()
    return raw or DEFAULT_EVIDENCE_ANCHOR_GIT_DIR


def evidence_anchor_git_signer_fingerprint() -> str | None:
    """Pinned Git witness signer, independent of the witness repo's trust config.

    SSH keys use ``SHA256:...``; GPG keys use the full hexadecimal fingerprint.
    An unsigned or unpinned commit cannot count as an external witness.
    """
    return os.environ.get("UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT", "").strip() or None
