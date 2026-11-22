"""`unified-agent node`: the device side of the ua-node contract (task #1327, I1).

The node connects OUT to the hub — HTTP over the tailnet, no listening port on
the device. It authenticates with the device token `unified-agent login` saved
(RFC 8628 device grant, task #1320), refreshing it through
``device_login.stored_access_token`` as it nears expiry. Then it:

1. registers host facts: OS, harness binaries found on PATH with versions, and
   worktree roots;
2. heartbeats on an interval from a background thread, so the Hosts page shows
   a measured ``reported: true`` row instead of an MCP-identity guess, and so a
   running item's lease stays renewed even while a backend blocks the main loop;
3. long-polls its own dispatch queue;
4. runs each claimed item through an existing executor backend
   (``executor.backends.get_backend``), with the backend's ``kill_check`` wired to
   SIGTERM and to hub-relayed cancellation;
5. posts exactly one receipt per item. Receipts are idempotent on the hub, so a
   receipt whose response was lost is simply resent until the hub acknowledges it.
6. with ``UA_NODE_WAKE=1`` only, runs a wake loop thread beside the heartbeat:
   it long-polls ``/api/node/wake`` and types the fixed wake line into idle
   TUIOS or herdr agents through ``node_wake_transport`` (tasks #1873, #2376).

Connection failures back off exponentially with jitter. SIGTERM stops the loop,
kills the running child through ``kill_check``, posts its receipt, and exits.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import logging
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    acceptance_backend,
    acp_matrix,
    config,
    device_key,
    device_login,
    harness_config,
    hub_runtime,
    native_sessions,
    node_claim_journal,
)

logger = logging.getLogger("unified_agent.node")

# Transport contract: POST ``path`` with a JSON body, return (status, json_body).
# Raise ``NodeTransportError`` when the hub could not be reached at all.
Transport = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]]

# Pi is deliberately not a default: PiBackend accepts kill_check but cannot yet
# interrupt its child (task #383 follow-up), so SIGTERM and hub cancellation would
# not reach a running Pi item. Opt in with UA_NODE_BACKENDS=codex,agy,pi.
DEFAULT_BACKENDS = ("codex", "agy")
# The built-in, no-model acceptance job (task #2112). Every node advertises
# it, whatever UA_NODE_BACKENDS says: it runs no harness, so there is nothing
# to install, and the hub needs it to accept a device before real work.
ACCEPTANCE_BACKEND = acceptance_backend.BACKEND
DEFAULT_HEARTBEAT_S = 30.0
# Session-scoped hub MCP for ACP sessions (task #1744). Same string as the
# hub's ``session_credentials.FEATURE``; kept here so the node does not import
# the hub-side module.
SESSION_MCP_FEATURE = "acp_session_mcp"
# Hub stores a heartbeat ``code_sha`` fact (task #1946). Same string as the
# hub's ``node_registry.CODE_SHA_FEATURE``.
CODE_SHA_FEATURE = "node_code_sha"
# Same contract string as node_registry; do not import the hub-side module.
CWD_REALPATH_FEATURE = "cwd_realpath"
# Hub serves /api/node/wake (task #1873). Same string as node_registry.WAKE_FEATURE.
WAKE_FEATURE = "node_wake_v3"
# Hub reads ``live_sessions`` on the wake poll (task #2428). Same string as node_registry.
LIVE_SESSIONS_FEATURE = "node_wake_live_sessions"
#: Between pane reports; several fit inside the hub's 30-minute abandon window.
LIVE_REPORT_S = 300.0
#: Same caps as the hub's ``MAX_LIVE_SESSIONS`` and ``MAX_EXTERNAL_ID_CHARS``.
MAX_LIVE_REPORT = 64
MAX_LIVE_ID_CHARS = 512
# Reported in registered facts; the hub refuses follow-ups for a node without it.
NODE_FEATURES = (
    acp_matrix.FOLLOWUP_FEATURE,
    acp_matrix.RESUME_FEATURE,
    "session_settings",
    SESSION_MCP_FEATURE,
    CWD_REALPATH_FEATURE,
)
SESSION_CREDENTIAL_PATH = "/api/node/session_credential"
SESSION_CREDENTIAL_TIMEOUT_S = 15.0
# Hub refusal reasons the node records as-is in ``hub_mcp_reason``; anything
# else is recorded as ``mint_refused`` with the hub's reason as detail.
_MINT_REASONS = {"disabled": "hub_disabled", "node_not_allowed": "node_not_allowed"}
_CREDENTIAL_SHAPE = re.compile(r"^uas_[A-Za-z0-9_-]{43}$")


def node_features() -> list[str]:
    """Features this node advertises; ``UA_NODE_SESSION_MCP=0`` drops session MCP."""
    features = list(NODE_FEATURES)
    if not config.node_session_mcp_enabled():
        features.remove(SESSION_MCP_FEATURE)
    return features


# Native session discovery (task #1741): rescanned at most this often and
# reported on the next heartbeat; UA_NODE_DISCOVERY_S=0 turns it off.
DEFAULT_DISCOVERY_S = 300.0
# The hub refuses a facts payload over this many bytes (node_queue.MAX_FACTS_BYTES);
# the node trims its discovered sessions to fit before it sends one.
MAX_FACTS_BYTES = 64_000
# How long a finished ACP session stays up waiting for a follow-up before the
# idle reaper stops it (it stays resumable). The node runs one item at a
# time, so this also delays the next claim; UA_NODE_ACP_IDLE_S overrides.
DEFAULT_ACP_IDLE_S = 60.0
# Native backends that can continue a session through their own CLI flag.
NATIVE_RESUME_BACKENDS = frozenset({"claude", "codex"})
# Native session fingerprints kept on disk (oldest dropped first).
MAX_FINGERPRINTS = 500
DEFAULT_CLAIM_WAIT_S = 25.0
# Hub serves durable claim operations (task #2136). Same string as the hub's
# node_claim_ops.FEATURE; a node claims only from a hub that advertises it.
CLAIM_OP_FEATURE = node_claim_journal.FEATURE
# How long a node whose claim journal is held, busy or unusable waits before
# it looks again. A hold never claims new work and never relaunches.
CLAIM_HOLD_S = 60.0
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
AUTH_BACKOFF_S = 300.0
# A node an operator retired (task #2181) cannot fix itself: re-asking often
# only fills the log. The hub's refusal carries this code.
RETIRED_BACKOFF_S = 1800.0
RETIRED_CODE = "node_retired"
# The device's owner agent is paused or terminated (task #2315). Unlike a
# refused credential this lifts on its own when an operator resumes the agent,
# so the node stays registered and re-asks on the claim-hold interval.
AGENT_STOPPED_CODES = frozenset({"agent_paused", "agent_terminated"})
HARNESS_VERSION_TIMEOUT_S = 5.0
# Hub-mediated ACP permission polling: how often the node re-asks
# /api/node/permission while a decision is pending, and the overall cap
# before the agent is answered cancelled (fail-closed either way).
ACP_PERMISSION_POLL_S = 2.0
ACP_PERMISSION_MAX_S = 300.0

# backend name → (env override, default binary). Mirrors executor.backends so a
# node advertises a backend only when the binary that backend would exec exists.
HARNESS_BINS: dict[str, tuple[str | None, str]] = {
    "codex": ("UA_CODEX_BIN", "codex"),
    "agy": ("UA_AGY_BIN", "agy"),
    "pi": (None, "pi"),
    "claude": ("UA_CLAUDE_BIN", "claude"),
    "cursor": ("UA_CURSOR_BIN", "agent-with-hub"),
    "hermes": ("UA_HERMES_BIN", "hermes"),
    "copilot": ("UA_COPILOT_BIN", "copilot-with-hub"),
    "devin": ("UA_DEVIN_BIN", "devin-with-hub"),
}

_NODE_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class NodeTransportError(RuntimeError):
    """The hub could not be reached, or answered 5xx. Transient: back off, retry."""


class NodeAuthError(RuntimeError):
    """The hub refused the device credential (401/403). Needs `unified-agent login`."""


class NodeRetiredError(NodeAuthError):
    """An operator retired this node name (task #2181). A login cannot fix it:
    an operator must run ``node_unretire``, or the device registers under a
    new node name. A subclass of ``NodeAuthError`` so every loop that stops on
    a refused credential also stops here."""


class NodeRequestError(RuntimeError):
    """The hub rejected one request (4xx). Not retried as-is."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class NodeAgentStopped(NodeRequestError):
    """The hub refused work because the owner agent is paused or terminated (task #2315).

    A ``NodeRequestError`` (403) so callers that already treat a refused request
    as final keep doing so; the worker loop waits for a resume instead of
    dropping its registration as it would for a refused credential."""


class NodeClaimHeld(RuntimeError):
    """Claiming is on hold: an outstanding claim operation needs reconciliation
    (or the operator), so the node neither claims new work nor relaunches."""


# --- transport ------------------------------------------------------------------


#: Largest hub reply the node will read. The biggest legitimate one is a harness
#: config bundle (512 KB) or a follow-up batch (20 x 20 000 characters).
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _bounded_read(resp: Any) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES``; a longer reply is refused, not buffered."""
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError(f"hub reply exceeds {MAX_RESPONSE_BYTES} bytes")
    return data


class HubTransport:
    """JSON-over-HTTP to the hub's ``/api/node/*`` routes with the device token."""

    def __init__(
        self,
        hub: str,
        *,
        token_path: Path | None = None,
        allow_insecure: bool = False,
        token_fn: Callable[[], str | None] | None = None,
    ):
        self.hub = device_login.hub_origin(hub)
        if not allow_insecure and not device_login.transport_is_protected(self.hub):
            raise NodeAuthError(
                "refusing to send a device token over plain HTTP to a non-tailnet host; "
                "use https:// (or --allow-insecure for a trusted lab network)"
            )
        self._token_path = token_path
        self._token_fn = token_fn

    def _token(self) -> str | None:
        if self._token_fn is not None:
            return self._token_fn()
        # Re-read on every call: stored_access_token refreshes near expiry and
        # serializes refresh with any other process sharing the sign-in.
        return device_login.stored_access_token(self._token_path, hub=self.hub) or (
            os.environ.get("UA_AGENT_BEARER_TOKEN") or None
        )

    def __call__(
        self, path: str, body: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any]]:
        token = self._token()
        if not token:
            raise NodeAuthError(
                f"no device sign-in for {self.hub}; run `unified-agent login --hub {self.hub}`"
            )
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        # Hosted sign-ins are bound to the device key beside the token file.
        # Tailnet sign-ins have no key file, so they send the bearer alone.
        token_path = (
            self._token_path if self._token_path is not None else device_login.default_token_path()
        )
        try:
            proof = device_key.proof_header(
                token_path, method="POST", path=path, access_token=token, body=data
            )
        except device_key.DeviceKeyError as exc:
            raise NodeAuthError(str(exc)) from exc
        if proof:
            headers[device_key.PROOF_HEADER] = proof
        req = urllib.request.Request(
            f"{self.hub}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with device_login.open_no_redirect(req, timeout=timeout) as resp:
                return resp.status, json.loads(_bounded_read(resp) or b"{}")
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(_bounded_read(err) or b"{}")
            except ValueError:
                payload = {"error": f"http_{err.code}"}
            return err.code, payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise NodeTransportError(f"hub unreachable at {self.hub}: {err}") from err


# --- backoff --------------------------------------------------------------------


class Backoff:
    """Exponential backoff with equal jitter: half the step fixed, half random.

    Equal (not full) jitter keeps a floor under each wait so a fleet of nodes
    restarting together still spreads out without ever retrying immediately.
    """

    def __init__(
        self,
        *,
        base_s: float = BACKOFF_BASE_S,
        cap_s: float = BACKOFF_CAP_S,
        rng: random.Random | None = None,
    ):
        self.base_s = base_s
        self.cap_s = cap_s
        self.rng = rng or random.Random()
        self.failures = 0

    def next_delay(self) -> float:
        step = min(self.cap_s, self.base_s * (2**self.failures))
        self.failures += 1
        return step / 2 + self.rng.uniform(0, step / 2)

    def reset(self) -> None:
        self.failures = 0


# --- host facts -----------------------------------------------------------------


def default_node_name() -> str:
    """``UA_NODE_NAME``, else the same host key system_doctor stamps (UA_SOURCE_HOST
    or hostname), so a node row merges with that host's doctor snapshot."""
    raw = os.environ.get("UA_NODE_NAME") or config.current_source_host()
    name = _NODE_NAME_SAFE.sub("-", raw.strip()).strip("-.") or "node"
    return name[:128]


def default_worktree_roots() -> list[Path]:
    raw = os.environ.get("UA_NODE_WORKTREE_ROOTS", "")
    parts = [p for p in raw.split(os.pathsep) if p.strip()] or ["~/dev"]
    return [Path(p).expanduser() for p in parts]


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", name, raw)
        return default


def fit_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Drop the oldest discovered sessions until ``facts`` fits the hub's cap."""
    sessions = facts.get("native_sessions")
    if not isinstance(sessions, list):
        return facts
    fitted = {**facts, "native_sessions": list(sessions)}
    while (
        fitted["native_sessions"]
        and len(json.dumps(fitted, default=str).encode("utf-8")) > MAX_FACTS_BYTES
    ):
        fitted["native_sessions"].pop()
    return fitted


def discovery_interval_s() -> float:
    return _env_seconds("UA_NODE_DISCOVERY_S", DEFAULT_DISCOVERY_S)


def acp_idle_s() -> float:
    return _env_seconds("UA_NODE_ACP_IDLE_S", DEFAULT_ACP_IDLE_S)


def allowed_backends() -> tuple[str, ...]:
    raw = os.environ.get("UA_NODE_BACKENDS", "")
    names = [b.strip().lower() for b in raw.split(",") if b.strip()]
    if not names:
        return DEFAULT_BACKENDS
    kept: list[str] = []
    for name in names:
        # acp:<harness> entries must exist in the launch matrix; a typo would
        # otherwise advertise a backend that can never be constructed.
        harness = acp_matrix.acp_backend_name(name)
        if harness is not None and harness not in acp_matrix.ACP_HARNESSES:
            logger.warning("dropping unknown ACP backend %r from UA_NODE_BACKENDS", name)
            continue
        if name == COMMAND_BACKEND and not allowed_commands():
            # A command node with nothing allowlisted could run nothing; never
            # advertise it (task #2413).
            logger.warning("dropping 'command' from UA_NODE_BACKENDS: UA_NODE_COMMANDS is empty")
            continue
        kept.append(name)
    return tuple(kept)


#: Supervised argv commands (task #2413): off unless the operator lists both the
#: backend in UA_NODE_BACKENDS and each permitted program in UA_NODE_COMMANDS.
COMMAND_BACKEND = "command"
COMMAND_LOG_NAME = "command.log"


def allowed_commands() -> frozenset[str]:
    """``argv[0]`` values this node may run, exactly as listed (no basename match)."""
    raw = os.environ.get("UA_NODE_COMMANDS", "")
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


def _count_worktrees(root: Path) -> int:
    """Task worktrees under ``<root>/<repo>/.worktrees/*``. Cheap, no git calls."""
    try:
        return sum(
            1
            for repo in root.iterdir()
            if (repo / ".worktrees").is_dir()
            for entry in (repo / ".worktrees").iterdir()
            if entry.is_dir()
        )
    except OSError:
        return 0


MAX_REPORTED_REPOS = 64


def _applied_config_state(
    bundle: dict[str, Any], version: str, *, drifted: set[str], applied: list[dict[str, Any]]
) -> dict[str, Any]:
    """Surfaces in sync with the bundle after this sync, and the skills they deliver.

    Rules surfaces (CLAUDE.md, AGENTS.md, .cursorrules) carry no skills; a
    skill counts as applied only when a skill-bearing surface is in sync, and
    ``skill_surfaces`` names which ones, so a seat is judged by its own harness.
    """
    surfaces = bundle.get("surfaces") or harness_config.DEFAULT_SURFACES
    in_sync = (set(surfaces) - drifted) | {str(item.get("surface")) for item in applied}
    # A skill-bearing surface is named for the harness it configures, so a
    # plan can check the seat's own harness instead of "any surface".
    delivering = sorted(in_sync & harness_config.skill_surfaces(surfaces))
    skills = sorted(
        str(skill.get("name"))
        for skill in bundle.get("skills") or []
        if isinstance(skill, dict) and skill.get("name")
    )
    return {
        "bundle_version": version,
        "in_sync": sorted(in_sync),
        "skill_surfaces": delivering,
        "skills": skills if delivering else [],
    }


def collect_repos(roots: list[Path], *, limit: int = MAX_REPORTED_REPOS) -> list[dict[str, str]]:
    """Git checkouts directly under each worktree root, with resolved paths (#1736).

    The hub never sees this filesystem, so a team plan can only trust a seat's
    workspace when the node itself reports the checkout and where it really
    resolves. ``realpath`` exposes a symlink that leaves its root.
    """
    out: list[dict[str, str]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            root_real = os.path.realpath(root)
            children = sorted(root.iterdir(), key=lambda child: child.name)
        except OSError:
            continue
        for child in children:
            if len(out) >= limit:
                return out
            try:
                if not (child / ".git").exists():
                    continue
                out.append(
                    {
                        "root": str(root),
                        "root_realpath": root_real,
                        "name": child.name,
                        "path": str(child),
                        "realpath": os.path.realpath(child),
                    }
                )
            except OSError:
                continue
    return out


def _harness_version(path: str, run: Callable[..., Any]) -> str | None:
    try:
        proc = run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=HARNESS_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:120] if text else None


_SURFACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
MAX_SURFACES = 50


def hermes_home() -> Path:
    default = Path.home() / (".hermes" + os.environ.get("HERMES_DATA_DIR_SUFFIX", ""))
    home = Path(os.path.expandvars(os.environ.get("HERMES_HOME") or str(default))).expanduser()
    # Hermes resolves an env home under its native root back to that root;
    # custom named homes resolve to their own root before selecting -p.
    try:
        home.resolve().relative_to(default.resolve())
        return default
    except ValueError:
        return home.parent.parent if home.parent.name == "profiles" else home


def collect_surfaces(home: Path | None = None) -> dict[str, list[str]]:
    """Hermes profile names and kanban assignee names on this host (task #1391).

    Names only: profile directory names under ``<HERMES_HOME>/profiles`` and
    the distinct ``assignee`` values of the local kanban database, read
    read-only. No config contents, no task text, no secrets. A name that is
    not a plain identifier is dropped rather than reported. Missing Hermes
    yields empty lists.
    """
    root = home or hermes_home()
    profiles: set[str] = {"default"} if root.is_dir() else set()
    # Match current Hermes name-only discovery: live identity markers and no
    # tombstones. Stat only; never load config, credentials or sessions.
    markers = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
    try:
        for entry in (root / "profiles").iterdir():
            if not acp_matrix.HERMES_PROFILE_RE.fullmatch(entry.name):
                continue
            if (
                entry.name != "default"
                and entry.is_dir()
                and not entry.is_symlink()
                and not (root / "profiles" / ".deleted" / entry.name).exists()
                and any(
                    (entry / marker).is_file() or (entry / marker).is_symlink()
                    for marker in markers
                )
            ):
                profiles.add(entry.name)
    except OSError:
        pass
    assignees: set[str] = set()
    db = root / "kanban.db"
    if db.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                for (name,) in conn.execute(
                    "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL"
                ):
                    if isinstance(name, str) and _SURFACE_NAME.match(name.strip()):
                        assignees.add(name.strip())
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - surfaces are advisory facts
            pass
    # Keep the live default selectable even when named profiles sort before it.
    default_profile = ["default"] if "default" in profiles else []
    bounded_profiles = sorted(profiles - {"default"})[: MAX_SURFACES - len(default_profile)]
    return {
        "hermes_profiles": sorted([*bounded_profiles, *default_profile]),
        "kanban_assignees": sorted(assignees)[:MAX_SURFACES],
    }


def collect_facts(
    *,
    roots: list[Path],
    backends: tuple[str, ...],
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Measured host facts. Only binaries actually on PATH are reported."""
    harnesses: dict[str, dict[str, Any]] = {}
    for backend, (env_name, default_bin) in HARNESS_BINS.items():
        binary = (os.environ.get(env_name) if env_name else None) or default_bin
        path = which(binary)
        if path:
            harnesses[backend] = {
                "bin": binary,
                "path": path,
                "version": _harness_version(path, run),
            }
    # acp:<harness> backends resolve through the ACP launch matrix — the ACP
    # binary may differ from the native one (e.g. claude-code-acp), so probe
    # each requested acp:* name on its own and advertise it only when the
    # binary actually resolves.
    for backend in backends:
        harness = acp_matrix.acp_backend_name(backend)
        if harness is None or harness not in acp_matrix.ACP_HARNESSES:
            continue
        try:
            argv = acp_matrix.resolve_argv(harness)
        except ValueError:
            continue
        binary = argv[0]
        # Absolute / path-shaped overrides (UA_*_BIN) must resolve as files;
        # shutil.which only finds PATH entries and would drop a valid harness.
        if os.path.isabs(binary) or os.sep in binary:
            path = binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
        else:
            path = which(binary)
        if path:
            harnesses[backend] = {
                "bin": argv[0],
                "argv": argv,
                "path": path,
                "version": _harness_version(path, run),
                "acp": True,
            }
    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "harnesses": harnesses,
        # The acceptance job is built in: no binary to probe (task #2112).
        "backends": [b for b in backends if b in harnesses or b == ACCEPTANCE_BACKEND],
        "worktree_roots": [
            {
                "path": str(root),
                # The resolved root (#1741): discovered session cwds are
                # resolved paths, so the hub re-checks them against this too.
                "realpath": os.path.realpath(root),
                "exists": root.is_dir(),
                "worktrees": _count_worktrees(root),
            }
            for root in roots
        ],
        "repos": collect_repos(roots),
        "surfaces": collect_surfaces(),
        # Protocol features this build implements, so the hub only offers what
        # the node will honour (task #1740: operator follow-ups for ACP runs).
        "node_features": node_features(),
        # Native Claude Code / Codex sessions under the roots (task #1741):
        # metadata only, never transcript content.
        "native_sessions": native_sessions.discover(roots) if discovery_interval_s() > 0 else [],
    }


VAULT_SYNC_PROBE_S = 300.0


def collect_vault_sync_state() -> dict[str, Any] | None:
    """This host's vault_sync doctor row, reported on heartbeats (#1813).

    The desktop pulls the hub vault on a local timer, so only the desktop can
    see that timer fail. Reusing doctor's own check keeps one definition of
    healthy. A host that does not run the sync reports DORMANT. A probe that
    raises reports UNKNOWN, never nothing: an omitted fact would leave the
    hub holding this node's last report, possibly a stale LIVE.
    """
    as_of = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        from .tools.doctor import vault_sync_status

        row = vault_sync_status()
    except Exception as exc:  # noqa: BLE001 - advisory fact, never fatal to a heartbeat
        return {"status": "UNKNOWN", "detail": f"vault sync probe failed: {exc}", "as_of": as_of}
    return {"status": row.get("status"), "detail": str(row.get("detail") or ""), "as_of": as_of}


@functools.cache
def code_sha(root: Path | None = None) -> str | None:
    """Commit the running code was imported from (task #1946), or None.

    ``_package_version()`` does not change per commit, so a node twelve days
    behind the hub looked current. This reads the checkout HEAD the package
    was imported from (``hub_runtime.checkout_head``: plain file reads, no
    ``git`` subprocess, a worktree's ``.git`` file followed). It is resolved
    once per process, so a later ``git pull`` that the process has not loaded
    does not change what it reports. None for a non-git install.
    """
    try:
        return hub_runtime.checkout_head(root or hub_runtime.package_checkout())
    except Exception:  # noqa: BLE001 - advisory fact, never fatal
        return None


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("unified-agent")
    except Exception:  # noqa: BLE001
        return "unknown"


# --- the node -------------------------------------------------------------------


class NodeAgent:
    """One device's connection to the hub. Every side effect is injectable."""

    def __init__(
        self,
        *,
        node: str,
        transport: Transport,
        roots: list[Path] | None = None,
        backends: tuple[str, ...] | None = None,
        backend_factory: Callable[..., Any] | None = None,
        facts_fn: Callable[[], dict[str, Any]] | None = None,
        sleep: Callable[[float], Any] | None = None,
        rng: random.Random | None = None,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        claim_wait_s: float = DEFAULT_CLAIM_WAIT_S,
        session_root: Path | None = None,
        spool_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        vault_sync_fn: Callable[[], dict[str, Any] | None] | None = None,
        hub_url: str | None = None,
        wake_transport: Any = None,
        acceptance_sleep: Callable[[float], Any] | None = None,
        claim_journal_dir: Path | None = None,
    ):
        self.node = node
        self.transport = transport
        # Origin the harness reaches the hub's /mcp at (task #1744): the
        # transport's own hub, which already passed transport_is_protected.
        self.hub_url = hub_url or getattr(transport, "hub", None)
        # Features the hub said it serves at registration (task #1744).
        self.hub_features: frozenset[str] = frozenset()
        self.roots = roots if roots is not None else default_worktree_roots()
        chosen = backends if backends is not None else allowed_backends()
        # Always allowed (task #2112): the acceptance job starts no harness.
        self.backends = tuple(dict.fromkeys((*chosen, ACCEPTANCE_BACKEND)))
        self._backend_factory = backend_factory
        self._facts_fn = facts_fn or (
            lambda: collect_facts(roots=self.roots, backends=self.backends)
        )
        self.stop_event = threading.Event()
        # Default sleep is the stop event, so SIGTERM interrupts any backoff wait.
        self._sleep = sleep or (lambda s: self.stop_event.wait(s))
        # The acceptance job's sleep slices (task #2112); SIGTERM ends them too.
        self._acceptance_sleep = acceptance_sleep or (lambda s: self.stop_event.wait(s))
        self.backoff = Backoff(rng=rng)
        self.heartbeat_s = heartbeat_s
        self.claim_wait_s = claim_wait_s
        self.session_root = session_root or (
            Path(os.environ.get("UA_HOME") or "~/.unified_agent").expanduser() / "node-sessions"
        )
        # Receipts are spooled to disk before they are sent, so a result survives
        # a crash or restart between the backend finishing and the hub's ack.
        self.spool_dir = spool_dir or (
            Path(os.environ.get("UA_HOME") or "~/.unified_agent").expanduser() / "node-receipts"
        )
        self.clock = clock
        self.registered = False
        self.agent: str | None = None
        self.running_item: str | None = None
        self._cancelled: set[str] = set()
        # item id -> attempt of every receipt this process produced or found
        # spooled: an outcome established locally, which is what resolves a
        # claim journal left at start_intent (task #2136).
        self._local_outcomes: dict[str, Any] = {}
        self._pending_receipts: list[tuple[str, dict[str, Any]]] = self._load_spool()
        # The durable claim journal (task #2136): one outstanding logical claim
        # operation per (hub, node), published before its claim is sent.
        self._claim_journal = node_claim_journal.ClaimJournal(
            claim_journal_dir or (self.spool_dir / "claim-journal"),
            hub=str(self.hub_url or ""),
            node=node,
        )
        self._held_announced: str | None = None
        self._retired_announced: str | None = None
        self._lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        # Idle-session wakes (task #1873): off unless UA_NODE_WAKE=1.
        self._wake_transport = wake_transport
        self._wake_thread: threading.Thread | None = None
        self._wake_announced: dict[str, int] = {}
        self._live_reported_at: float | None = None
        self._pending_wake_receipts = self._load_wake_receipts()
        # I4: last successful config sync (monotonic clock); throttled so a busy
        # claim loop does not hammer the hub on every idle tick.
        self._config_root = Path(
            os.environ.get("UA_HARNESS_CONFIG_ROOT") or str(harness_config.config_root())
        ).expanduser()
        self._last_config_sync = 0.0
        # What the last harness-config sync found applied on this device
        # (#1736): reported on heartbeats so a team plan can tell whether a
        # seat's declared skills are really present here.
        self._config_state: dict[str, Any] | None = None
        self._config_sync_s = float(os.environ.get("UA_HARNESS_CONFIG_SYNC_S") or "120")
        # The vault_sync probe shells out to systemctl, so it runs at most once
        # per VAULT_SYNC_PROBE_S and the last result rides every heartbeat.
        self._vault_sync_fn = vault_sync_fn or collect_vault_sync_state
        self._vault_sync_state: dict[str, Any] | None = None
        self._vault_sync_probed_at: float | None = None
        # Native session discovery (task #1741). The clock starts only when a
        # scan reaches the hub (registration facts that carry one, or a
        # heartbeat), so an unscanned or failed start rescans on the next beat.
        self._discovery_s = discovery_interval_s()
        self._discovered_at: float | None = None
        self._scan_pending_at: float | None = None
        # (mtime, size) of each native session file as this node's own last
        # run left it, so its next resume is not mistaken for a terminal.
        self._fingerprints_path = self.session_root / "native-fingerprints.json"
        self._discover_fn = lambda: native_sessions.discover(self.roots)

    # -- receipt spool --

    def _spool_path(self, item_id: str) -> Path:
        return self.spool_dir / f"{item_id}.json"

    def _load_spool(self) -> list[tuple[str, dict[str, Any]]]:
        """Receipts a previous run produced but never saw acknowledged."""
        pending: list[tuple[str, dict[str, Any]]] = []
        try:
            files = sorted(self.spool_dir.glob("nd-*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return pending
        for path in files:
            try:
                receipt = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(receipt, dict):
                pending.append((path.stem, receipt))
                self._local_outcomes[path.stem] = receipt.get("attempt")
        if pending:
            logger.info("resending %d spooled receipt(s)", len(pending))
        return pending

    def _spool(self, item_id: str, receipt: dict[str, Any]) -> bool:
        """Write the receipt durably (file and directory fsync). True when it is.

        Only a durable receipt counts as an established local outcome; on
        failure the receipt is still sent from memory, but the claim journal
        stays at ``start_intent`` until the hub acknowledges it (task #2136).
        """
        try:
            node_claim_journal.write_private_durable(self._spool_path(item_id), receipt)
        except OSError as exc:
            logger.warning("could not spool receipt for %s: %s", item_id, exc)
            return False
        self._local_outcomes[item_id] = receipt.get("attempt")
        return True

    def _unspool(self, item_id: str) -> None:
        try:
            self._spool_path(item_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove spooled receipt for %s: %s", item_id, exc)

    def _wake_receipt_path(self, offer_id: str) -> Path:
        return self.spool_dir / "wake" / f"{offer_id}.json"

    def _wake_cursor_path(self, sid: str) -> Path:
        key = hashlib.sha256(sid.encode()).hexdigest()
        return self.spool_dir / "wake-delivered" / f"{key}.json"

    def _load_wake_receipts(self) -> list[dict[str, Any]]:
        """Recover pending receipts and ambiguous crash-time attempts without retyping."""
        pending = []
        for path in sorted((self.spool_dir / "wake-delivered").glob("*.json")):
            try:
                row = json.loads(path.read_text())
                sid, seq = row["session_id"], row["seq"]
                if isinstance(sid, str) and type(seq) is int and seq >= 0:
                    self._wake_announced[sid] = max(seq, self._wake_announced.get(sid, 0))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in sorted((self.spool_dir / "wake").glob("*.json")):
            try:
                row = json.loads(path.read_text())
                sid, seq, offer_id = row["session_id"], row["seq"], row["offer_id"]
                if (
                    not isinstance(sid, str)
                    or type(seq) is not int
                    or seq < 0
                    or offer_id != path.stem
                ):
                    continue
                if not re.fullmatch(r"[a-f0-9]{32}", offer_id):
                    continue
                if row.pop("attempting", False):
                    row.update(outcome="skipped", reason="delivery_uncertain")
                if row["outcome"] == "delivered" or row.get("reason") == "delivery_uncertain":
                    self._wake_announced[sid] = max(seq, self._wake_announced.get(sid, 0))
                pending.append(row)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return pending

    def _flush_wake_receipts(self) -> bool:
        while self._pending_wake_receipts:
            body = self._pending_wake_receipts[0]
            sid, seq, offer_id = body["session_id"], body["seq"], body["offer_id"]
            # Persist the monotonic local cursor before discarding a delivered
            # receipt. A failed final write leaves the original spool for replay.
            if body["outcome"] == "delivered" or body.get("reason") == "delivery_uncertain":
                try:
                    device_login._write_private(
                        self._wake_cursor_path(sid),
                        {"session_id": sid, "seq": self._wake_announced.get(sid, seq)},
                    )
                except OSError:
                    return False
            try:
                self._call("/api/node/wake/receipt", body, timeout=15)
            except (NodeTransportError, NodeAuthError):
                return False
            except NodeRequestError as exc:
                # A permanent refusal does not justify typing an already
                # announced/ambiguous mail set again; the local cursor survives.
                logger.warning("wake receipt refused: %s", exc)
            try:
                self._wake_receipt_path(offer_id).unlink(missing_ok=True)
            except OSError:
                return False
            self._pending_wake_receipts.pop(0)
        return True

    # -- hub calls --

    def _call(self, path: str, body: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        status, payload = self.transport(path, {"node": self.node, **body}, timeout)
        if status in (200, 201):
            return payload
        error = str(payload.get("error") or f"http_{status}")
        if status == 403 and payload.get("code") == RETIRED_CODE:
            raise NodeRetiredError(error)
        if status == 403 and payload.get("code") in AGENT_STOPPED_CODES:
            raise NodeAgentStopped(error, status)
        if status in (401, 403):
            raise NodeAuthError(f"hub refused node credential ({status}): {error}")
        if status == 404 and "not registered" in error:
            # Hub DB reset or node row pruned: re-register on the next iteration.
            self.registered = False
            raise NodeTransportError(error)
        if status >= 500 or status in (0, 499):
            raise NodeTransportError(f"hub error {status}: {error}")
        raise NodeRequestError(error, status)

    def register(self) -> dict[str, Any]:
        facts = self._facts_fn()
        if isinstance(facts, dict):
            facts = fit_facts({**facts, "code_sha": code_sha()})
        result = self._call("/api/node/register", {"facts": facts, "version": _package_version()})
        if isinstance(facts, dict) and isinstance(facts.get("native_sessions"), list):
            self._discovered_at = time.monotonic()
        self.registered = True
        self.agent = result.get("agent")
        features = result.get("hub_features")
        self.hub_features = frozenset(
            f for f in (features if isinstance(features, list) else []) if isinstance(f, str)
        )
        logger.info("registered node %s as %s", self.node, self.agent)
        return result

    def _heartbeat_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"surfaces": collect_surfaces()}
        if self._config_state is not None:
            facts["harness_config"] = self._config_state
        now = time.monotonic()
        if (
            self._vault_sync_probed_at is None
            or now - self._vault_sync_probed_at >= VAULT_SYNC_PROBE_S
        ):
            self._vault_sync_state = self._vault_sync_fn()
            self._vault_sync_probed_at = now
        if self._vault_sync_state is not None:
            facts["vault_sync"] = self._vault_sync_state
        if CODE_SHA_FEATURE in self.hub_features:
            # An older hub would treat the unknown key as a full fact set.
            facts["code_sha"] = code_sha()
        if self._discovery_s > 0 and (
            self._discovered_at is None or now - self._discovered_at >= self._discovery_s
        ):
            # Sent only when rescanned; the hub keeps the last list in between.
            try:
                facts["native_sessions"] = self._discover_fn()
            except Exception as exc:  # noqa: BLE001 - advisory, never fatal
                # The clock does not start on a failed scan: retry next beat.
                logger.debug("native session discovery failed: %s", exc)
            else:
                # Starts only once the heartbeat carrying it succeeds.
                self._scan_pending_at = now
        return fit_facts(facts)

    def heartbeat(self, *, progress: str | None = None) -> dict[str, Any]:
        with self._lock:
            running = self.running_item
        self._scan_pending_at = None
        result = self._call(
            "/api/node/heartbeat",
            {"running": running, "progress": progress, "facts": self._heartbeat_facts()},
        )
        if self._scan_pending_at is not None:
            # The hub has this scan: the discovery interval starts now (#1741
            # review); a failed call leaves it unset so the next beat rescans.
            self._discovered_at, self._scan_pending_at = self._scan_pending_at, None
        for item_id in result.get("cancel") or []:
            if item_id == running:
                logger.warning("hub requested cancellation of %s", item_id)
                with self._lock:
                    self._cancelled.add(item_id)
        return result

    def sync_config(self, *, force: bool = False) -> dict[str, Any] | None:
        """Pull the hub harness bundle, report drift, apply approved grants only.

        Proposal-mode is the default: local files under ``UA_HARNESS_CONFIG_ROOT``
        are never overwritten without a consumed ``harness_config_apply`` pass.
        """
        now = self.clock()
        if not force and (now - self._last_config_sync) < self._config_sync_s:
            return None
        payload = self._call("/api/node/config", {"action": "get"})
        self._last_config_sync = now
        bundle = payload.get("bundle")
        if not isinstance(bundle, dict):
            logger.debug("no active harness config bundle on hub")
            self._config_state = None
            return {"status": "no_bundle"}
        version = str(payload.get("version") or bundle.get("version") or "")
        # Prefer hub-rendered content when present; fall back to local render.
        rendered = payload.get("rendered") if isinstance(payload.get("rendered"), dict) else {}
        proposals: list[dict[str, Any]] = []
        try:
            local = harness_config.compute_local_drift(bundle, root=self._config_root)
        except harness_config.HarnessConfigError as exc:
            logger.warning("harness config drift compute failed: %s", exc)
            return {"status": "error", "error": str(exc)}
        for item in local:
            surface = item["surface"]
            # Prefer hub content hash if the hub sent content for this surface.
            hub_info = rendered.get(surface) if isinstance(rendered.get(surface), dict) else {}
            if hub_info.get("hash") and hub_info.get("content") is not None:
                item = {
                    **item,
                    "desired_hash": hub_info["hash"],
                    "desired": hub_info["content"],
                    "diff": harness_config.make_diff(
                        item["path"],
                        harness_config.read_observed(
                            harness_config.resolve_surface_path(
                                item["path"], root=self._config_root
                            )
                        ),
                        hub_info["content"],
                    ),
                }
            # Strip desired body from the wire report — hub re-renders from the
            # active bundle; only hashes + diff travel.
            proposals.append(
                {
                    "surface": item["surface"],
                    "path": item["path"],
                    "desired_hash": item["desired_hash"],
                    "observed_hash": item.get("observed_hash"),
                    "diff": item.get("diff") or "",
                }
            )
        report: dict[str, Any] = {"status": "in_sync", "bundle_version": version, "proposals": []}
        if proposals:
            report = self._call(
                "/api/node/config",
                {
                    "action": "report",
                    "bundle_version": version,
                    "proposals": proposals,
                },
            )
            report = {"status": "drift_reported", **report}
        # Apply only surfaces with an explicit grant.
        grants = list(report.get("apply_grants") or payload.get("apply_grants") or [])
        applied: list[dict[str, Any]] = []
        for grant in grants:
            if not isinstance(grant, dict):
                continue
            surface = str(grant.get("surface") or "")
            desired_hash = str(grant.get("desired_hash") or "")
            if not surface or not desired_hash:
                continue
            try:
                written = harness_config.apply_surface(bundle, surface, root=self._config_root)
                if written["hash"] != desired_hash:
                    logger.error("refuse apply for %s: rendered hash mismatch after write", surface)
                    continue
                ack = self._call(
                    "/api/node/config",
                    {
                        "action": "applied",
                        "surface": surface,
                        "desired_hash": desired_hash,
                    },
                )
                applied.append({"surface": surface, "ack": ack, "path": written["path"]})
                logger.info(
                    "applied harness config surface %s under %s (bundle %s)",
                    surface,
                    written["path"],
                    version,
                )
            except (harness_config.HarnessConfigError, NodeRequestError, NodeTransportError) as exc:
                logger.warning("could not apply harness config %s: %s", surface, exc)
        if applied:
            report["applied"] = applied
            report["status"] = "applied"
        self._config_state = _applied_config_state(
            bundle, version, drifted={item["surface"] for item in local}, applied=applied
        )
        return report

    def claim(self) -> dict[str, Any] | None:
        """One unjournaled claim call, kept for diagnostics and older callers.

        A lost response here cannot be reconciled. ``run_once`` never uses it:
        the worker loop claims through the durable journal (task #2136).
        """
        # The HTTP timeout must outlast the server-side long-poll.
        result = self._call(
            "/api/node/claim",
            {"wait_s": self.claim_wait_s},
            timeout=self.claim_wait_s + 15,
        )
        return result.get("item")

    # -- durable claim operations (task #2136) --

    def _claim_exchange(self, entry: dict[str, Any], *, mode: str) -> dict[str, Any]:
        """Send one claim or reconciliation for the journal's operation.

        Each call is a new HTTP request with a fresh device proof; only the
        logical operation id repeats. Any failure leaves the journal as it was,
        so the next iteration reconciles: an error is never read as "nothing
        was claimed".
        """
        wait_s = self.claim_wait_s if mode == "claim" else 0
        claim_op = {"id": entry["op_id"], "mode": mode, "issued_at": entry["issued_at"]}
        # The HTTP timeout must outlast the server-side long-poll.
        result = self._call(
            "/api/node/claim", {"wait_s": wait_s, "claim_op": claim_op}, timeout=wait_s + 15
        )
        disposition = result.get("claim_op")
        if (
            not isinstance(disposition, dict)
            or disposition.get("id") != entry["op_id"]
            or disposition.get("state") not in ("bound", "no_claim")
        ):
            raise NodeClaimHeld(
                f"hub answered claim operation {entry['op_id']} without a valid disposition"
            )
        return result

    def _accept_disposition(
        self, journal: node_claim_journal.ClaimJournal, entry: dict[str, Any], result: dict
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Persist the hub's disposition. Returns the item only if it may run."""
        disposition = result["claim_op"]
        item = result.get("item")
        if disposition["state"] == "no_claim":
            if entry["state"] == "ready":
                logger.warning(
                    "hub closed claim operation %s (%s); not starting %s",
                    entry["op_id"],
                    disposition.get("reason"),
                    entry.get("item_id"),
                )
            journal.clear(entry)
            return None, entry
        if disposition.get("executable") and not isinstance(item, dict):
            # The hub says the binding may run but sent no usable payload.
            # Clearing would forget a live claim: keep the journal and hold.
            raise NodeClaimHeld(
                f"hub reported claim operation {entry['op_id']} executable without a "
                "usable item; keeping the journal for reconciliation"
            )
        if not disposition.get("executable"):
            # Only pending/ready reach here: this worker never started it.
            logger.warning(
                "claim operation %s is bound to %s attempt %s but may not run (%s)",
                entry["op_id"],
                disposition.get("item_id"),
                disposition.get("attempt"),
                disposition.get("reason"),
            )
            journal.clear(entry)
            return None, entry
        item_id, attempt = item.get("item_id"), item.get("attempt")
        if (item_id, attempt) != (disposition.get("item_id"), disposition.get("attempt")) or (
            entry["state"] == "ready" and (item_id, attempt) != (entry["item_id"], entry["attempt"])
        ):
            raise NodeClaimHeld(
                f"hub returned a binding for claim operation {entry['op_id']} that does not "
                "match its disposition or the journal"
            )
        if not isinstance(item_id, str) or type(attempt) is not int:
            raise NodeClaimHeld(f"hub returned a malformed binding for {entry['op_id']}")
        return item, journal.mark_ready(entry, item_id=item_id, attempt=attempt)

    def _prune_local_outcomes(self, keep: str | None) -> None:
        """Bound ``_local_outcomes`` to unflushed receipts plus the journal's item."""
        live = {item_id for item_id, _ in self._pending_receipts}
        if keep is not None:
            live.add(keep)
        for item_id in [i for i in self._local_outcomes if i not in live]:
            del self._local_outcomes[item_id]

    def _hold(self, entry: dict[str, Any]) -> NodeClaimHeld:
        message = (
            f"claim operation {entry['op_id']} for item {entry.get('item_id')} attempt "
            f"{entry.get('attempt')} may already have started and has no recorded outcome; "
            "not relaunching it and not claiming new work. Reconcile the item on the hub, "
            f"then remove {self._claim_journal.path} to resume this node"
        )
        return NodeClaimHeld(message)

    def _claim_and_run(self, journal: node_claim_journal.ClaimJournal) -> str:
        """Reconcile the outstanding operation, or start one; run what is bound.

        Runs under the exclusive journal lock, from the first read through the
        ``ready -> start_intent`` transition and the receipt spool.
        """
        if CLAIM_OP_FEATURE not in self.hub_features:
            # Fail closed: an older hub cannot reconcile a lost response. A
            # re-registration refreshes the features after a hub upgrade.
            self.registered = False
            raise NodeClaimHeld("hub does not serve claim reconciliation; not claiming")
        entry = journal.load()
        # Keep only outcomes still needed: receipts not yet acknowledged, and
        # the one a start_intent journal may need to resolve.
        self._prune_local_outcomes(entry.get("item_id") if entry is not None else None)
        if entry is not None and entry["state"] == "start_intent":
            if self._local_outcomes.get(entry["item_id"]) == entry["attempt"]:
                # The receipt exists: the outcome is established, not ambiguous.
                journal.clear(entry)
                return "reconciled"
            raise self._hold(entry)
        if entry is not None:
            result = self._claim_exchange(entry, mode="reconcile")
            self.backoff.reset()
            item, entry = self._accept_disposition(journal, entry, result)
            if item is None:
                return "reconciled"
            logger.info("reconciled claim %s: %s", entry["op_id"], item.get("item_id"))
        else:
            entry = journal.begin(issued_at=int(self.clock()))
            result = self._claim_exchange(entry, mode="claim")
            self.backoff.reset()
            item, entry = self._accept_disposition(journal, entry, result)
            if item is None:
                return "idle"
        entry = journal.mark_start_intent(entry)
        logger.info("claimed %s (%s)", item.get("item_id"), item.get("backend"))
        receipt = self.execute(item)
        # Fences a stale claim: the hub refuses a receipt from an attempt
        # that is no longer the item's latest claim.
        receipt["attempt"] = item.get("attempt")
        if isinstance(item.get("command"), dict):
            # A supervised command's receipt must echo its claim's fence (#2413).
            receipt["fence_token"] = item["command"].get("fence_token")
        item_id = str(item["item_id"])
        durable = self._spool(item_id, receipt)
        self._pending_receipts.append((item_id, receipt))
        if not durable:
            # No durable receipt: keep start_intent until the hub has the
            # receipt. A send failure propagates with the journal kept, so a
            # stop now leaves a held node rather than one that claims anew.
            self._flush_receipts()
            if self._local_outcomes.get(item_id) != receipt["attempt"]:
                raise NodeClaimHeld(f"receipt for {item_id} is neither spooled nor acknowledged")
        journal.clear(entry)
        return "ran"

    def post_receipt(self, item_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        return self._call("/api/node/receipt", {"item_id": item_id, "receipt": receipt})

    # -- ACP relays --

    def _event_poster(self, item_id: str) -> Callable[[dict[str, Any]], None]:
        """Best-effort per-event stream to the hub.

        Event posts never fail the run — a hub hiccup during a turn must not
        kill work the receipt can still describe. Failures are counted and
        logged for diagnosis.
        """
        stats = {"sent": 0, "dropped": 0}

        def post(event: dict[str, Any]) -> None:
            try:
                self._call("/api/node/events", {"item_id": item_id, "events": [event]}, timeout=15)
                stats["sent"] += 1
            except Exception as exc:  # noqa: BLE001 - events are advisory
                stats["dropped"] += 1
                if stats["dropped"] <= 3 or stats["dropped"] % 50 == 0:
                    logger.warning(
                        "event post failed for %s (%d dropped): %s",
                        item_id,
                        stats["dropped"],
                        exc,
                    )

        post.stats = stats  # type: ignore[attr-defined]
        return post

    def _followup_fetcher(self, item_id: str) -> Callable[[], list[dict[str, Any]]]:
        """Pull operator follow-ups for the running item (task #1740).

        Best-effort like the event stream: a failed poll returns nothing and
        the next poll retries. A hub that does not serve the route (404/403/405)
        turns polling off for this item instead of retrying every tick.
        """
        state: dict[str, Any] = {"enabled": True, "unacked": []}

        def fetch() -> list[dict[str, Any]]:
            if not state["enabled"]:
                return []
            # Acknowledge what earlier replies carried; the hub keeps handing a
            # follow-up out until it is acknowledged, so a lost reply loses
            # nothing (the backend dedupes by id).
            ack = list(state["unacked"][:50])
            try:
                result = self._call(
                    "/api/node/followups", {"item_id": item_id, "ack": ack}, timeout=10
                )
            except NodeRequestError as exc:
                if exc.status in (403, 404, 405):
                    state["enabled"] = False
                    logger.info("hub offers no follow-ups for %s (%s)", item_id, exc)
                return []
            except Exception as exc:  # noqa: BLE001 - follow-ups are advisory
                logger.debug("follow-up poll for %s failed: %s", item_id, exc)
                return []
            followups = result.get("followups") if isinstance(result, dict) else None
            if not isinstance(followups, list):
                return []
            state["unacked"] = [fid for fid in state["unacked"] if fid not in ack]
            for entry in followups[:50]:
                fid = entry.get("id") if isinstance(entry, dict) else None
                if isinstance(fid, str) and fid not in state["unacked"]:
                    state["unacked"].append(fid)
            return followups

        return fetch

    def _permission_relay(self, item_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """Block on a hub decision for one ACP permission prompt.

        Returns the hub's status dict (``approved``+``option_id`` /
        ``denied`` / ``pending``); pending re-polls until the cap; every
        transport failure returns a non-approved dict — the backend maps
        anything but an approved selection to the ACP ``cancelled`` outcome.
        Never auto-approves and never falls back to a local policy.
        """

        def relay(payload: dict[str, Any]) -> dict[str, Any]:
            body = {
                "item_id": item_id,
                "session_id": payload.get("session_id"),
                "tool_call": payload.get("tool_call"),
                "options": payload.get("options"),
            }
            deadline = self.clock() + ACP_PERMISSION_MAX_S
            while True:
                try:
                    result = self._call("/api/node/permission", body, timeout=15)
                except Exception as exc:  # noqa: BLE001 - fail closed
                    logger.warning(
                        "permission request for %s failed (%s); answering cancelled",
                        item_id,
                        exc,
                    )
                    return {"status": "error", "reason": f"hub_unreachable: {exc}"[:300]}
                if not isinstance(result, dict):
                    return {"status": "error", "reason": "malformed hub reply"}
                if result.get("status") == "pending" and not self.stop_event.is_set():
                    if self.clock() < deadline:
                        self._sleep(ACP_PERMISSION_POLL_S)
                        continue
                # approved/denied pass through verbatim; pending past the cap,
                # an unknown status, or a stopping node all return as-is — the
                # backend maps anything but approved+option to cancelled.
                return result

        return relay

    def _session_credential(self, item: dict[str, Any]) -> Callable[[], dict[str, Any]]:
        """Mint a session-scoped hub credential for this item, on demand (task #1744).

        The backend calls this after ``initialize`` and only when the agent
        advertises ``mcpCapabilities.http``. It returns ``{"status": "scoped",
        "credential", "credential_id", "url", ...}`` or ``{"status": "none",
        "reason"}``; it never raises, and a failure never fails the run. The
        value is returned to the caller and kept nowhere on this object; the
        hub response is never logged.
        """
        item_id = str(item.get("item_id") or "")
        attempt = item.get("attempt")

        def mint() -> dict[str, Any]:
            if not config.node_session_mcp_enabled():
                return {"status": "none", "reason": "node_opt_out"}
            if SESSION_MCP_FEATURE not in self.hub_features:
                return {"status": "none", "reason": "hub_unsupported"}
            if not self.hub_url:
                return {"status": "none", "reason": "hub_url_unknown"}
            if isinstance(attempt, bool) or not isinstance(attempt, int):
                return {"status": "none", "reason": "attempt_unknown"}
            body = {"node": self.node, "item_id": item_id, "attempt": attempt}
            try:
                status, payload = self.transport(
                    SESSION_CREDENTIAL_PATH, body, SESSION_CREDENTIAL_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 - fall back to no hub MCP
                logger.warning(
                    "session credential request for %s failed (%s)", item_id, type(exc).__name__
                )
                return {"status": "none", "reason": "mint_failed"}
            payload = payload if isinstance(payload, dict) else {}
            reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
            if status in (200, 201):
                credential = payload.get("credential")
                if not isinstance(credential, str) or not _CREDENTIAL_SHAPE.fullmatch(credential):
                    return {"status": "none", "reason": "mint_failed", "detail": "malformed"}
                return {
                    "status": "scoped",
                    "credential": credential,
                    "credential_id": payload.get("credential_id"),
                    "expires_at": payload.get("expires_at"),
                    "url": f"{str(self.hub_url).rstrip('/')}/mcp",
                    "name": payload.get("mcp_server_name") or "unified-agent",
                }
            if status in (404, 405) and reason is None:
                # A hub that predates the route.
                return {"status": "none", "reason": "hub_unsupported"}
            if reason in _MINT_REASONS:
                return {"status": "none", "reason": _MINT_REASONS[reason]}
            return {
                "status": "none",
                "reason": "mint_refused",
                "detail": (reason or f"http_{status}")[:64],
            }

        return mint

    # -- execution --

    def _cwd_allowed(self, cwd: str) -> Path | None:
        """The item's cwd, resolved, only when it sits inside a worktree root."""
        try:
            target = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not target.is_dir():
            return None
        for root in self.roots:
            try:
                target.relative_to(root.expanduser().resolve())
                return target
            except (OSError, ValueError):
                continue
        return None

    def _adopt_preflight(self, backend_name: str, session_id: str) -> str | None:
        """Re-check an adopted session on disk just before spawning (#1741 review).

        The hub judged the session quiet from the last discovery scan, which
        can be minutes old. A session written to within the quiet window may
        be open in a terminal again; resuming it would add a second writer, so
        the item is refused instead and nothing is spawned.
        """
        harness = acp_matrix.acp_backend_name(backend_name) or backend_name
        path = native_sessions.session_file(harness, session_id)
        if path is None:
            return "session_not_found"
        try:
            stat = path.stat()
        except OSError:
            return "session_not_found"
        quiet = native_sessions.adopt_quiet_s()
        if quiet <= 0:
            return None
        if self._own_write(harness, session_id, stat):
            # Unchanged since this node's own last run of the session: the
            # recent write is ours, not a terminal's (#1741 review).
            return None
        if self.clock() - stat.st_mtime < quiet:
            return "session_recently_active"
        return None

    # -- native session fingerprints --

    def _fingerprint_key(self, harness: str, session_id: str) -> str:
        return f"{harness}:{session_id}"

    def _load_fingerprints(self) -> dict[str, list[float]]:
        try:
            raw = json.loads(self._fingerprints_path.read_text())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _own_write(self, harness: str, session_id: str, stat: os.stat_result) -> bool:
        seen = self._load_fingerprints().get(self._fingerprint_key(harness, session_id))
        return (
            isinstance(seen, list)
            and len(seen) == 2
            and seen[0] == stat.st_mtime
            and seen[1] == stat.st_size
        )

    def _record_fingerprint(self, backend_name: str, session_id: str) -> None:
        """Remember the session file as this node's run left it (#1741 review).

        The next resume's preflight treats an unchanged (mtime, size) as this
        node's own write; any later change (a terminal) applies the quiet
        window again. Kept on disk so it survives a node restart; bounded.
        """
        harness = acp_matrix.acp_backend_name(backend_name) or backend_name
        if harness not in native_sessions.HARNESSES:
            return
        path = native_sessions.session_file(harness, session_id)
        if path is None:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        prints = self._load_fingerprints()
        prints.pop(self._fingerprint_key(harness, session_id), None)
        prints[self._fingerprint_key(harness, session_id)] = [stat.st_mtime, stat.st_size]
        while len(prints) > MAX_FINGERPRINTS:
            prints.pop(next(iter(prints)))
        try:
            device_login._write_private(self._fingerprints_path, prints)
        except OSError as exc:
            logger.warning("could not record native session fingerprint: %s", exc)

    def _refuse(self, reason: str) -> dict[str, Any]:
        now = _iso_now(self.clock)
        return {"status": "failed", "reason": reason, "started_at": now, "finished_at": now}

    def execute(self, item: dict[str, Any]) -> dict[str, Any]:
        """Run one claimed item; always returns a receipt, never raises."""
        item_id = str(item["item_id"])
        backend_name = str(item.get("backend") or "")
        if backend_name not in self.backends:
            return self._refuse(f"backend_not_allowed_on_node:{backend_name}")
        cwd = self._cwd_allowed(str(item.get("cwd") or ""))
        if cwd is None:
            return self._refuse("cwd_outside_worktree_roots")
        planned = item.get("cwd_realpath")
        if planned is not None and str(cwd) != str(planned):
            # Task #1770: the approval bound the checkout the cwd resolved to
            # when it was planned. A symlink retargeted since, even to another
            # directory inside the roots, is a different checkout.
            return self._refuse("cwd_realpath_changed")
        agent_label = str(item.get("agent") or self.agent or "")
        profile = item.get("hermes_profile")
        if backend_name == "acp:hermes":
            try:
                acp_matrix.validate_hermes_profile(profile)
            except ValueError:
                return self._refuse("hermes_profile_invalid")
            if profile not in collect_surfaces()["hermes_profiles"]:
                return self._refuse("hermes_profile_not_live_on_node")
            if not isinstance(item.get("profile_agent"), str) or not item["profile_agent"]:
                return self._refuse("hermes_profile_unmapped")
        elif profile is not None:
            return self._refuse("hermes_profile_backend_mismatch")
        resume_id = item.get("resume_session_id")
        is_acp = acp_matrix.acp_backend_name(backend_name) is not None
        if resume_id is not None:
            if not isinstance(resume_id, str) or not native_sessions.SESSION_ID_RE.fullmatch(
                resume_id
            ):
                return self._refuse("resume_session_id_invalid")
            if not is_acp and backend_name not in NATIVE_RESUME_BACKENDS:
                # Never fall back to a fresh run when a resume was asked for.
                return self._refuse(f"resume_unsupported_backend:{backend_name}")
            if item.get("resume_source") in ("adopt", "resume_native"):
                refusal = self._adopt_preflight(backend_name, resume_id)
                if refusal is not None:
                    return self._refuse(refusal)
        command_argv: list[str] | None = None
        if backend_name == COMMAND_BACKEND:
            spec = item.get("command") if isinstance(item.get("command"), dict) else {}
            argv = spec.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(a, str) and a and "\x00" not in a for a in argv)
            ):
                return self._refuse("command_argv_invalid")
            if argv[0] not in allowed_commands():
                return self._refuse(f"command_not_allowlisted:{argv[0]}"[:300])
            command_argv = list(argv)
        try:
            if backend_name == COMMAND_BACKEND:
                backend = None  # built below, once its log file has a directory
            elif backend_name == ACCEPTANCE_BACKEND:
                # Built in and never substitutable (task #2112): an injected or
                # registry backend could start a harness; this one cannot.
                backend = acceptance_backend.AcceptanceBackend(sleep=self._acceptance_sleep)
            else:
                factory = self._backend_factory or _default_backend_factory
                backend = factory(backend_name, agent_label=agent_label)
        except Exception as exc:  # noqa: BLE001 - reported in the receipt
            return self._refuse(f"backend_unavailable:{exc}"[:300])

        session_dir = self.session_root / item_id
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._refuse(f"session_dir_unavailable:{exc}"[:300])
        if command_argv is not None:
            from .executor.backends import CommandBackend

            backend = CommandBackend(command_argv, str(session_dir / COMMAND_LOG_NAME))

        observed_cancel = False

        def kill_check() -> bool:
            nonlocal observed_cancel
            with self._lock:
                observed_cancel = self.stop_event.is_set() or item_id in self._cancelled
                return observed_cancel

        started = _iso_now(self.clock)
        with self._lock:
            self.running_item = item_id
        try:
            run_kwargs: dict[str, Any] = {
                "prompt": str(item.get("prompt") or ""),
                "cwd": str(cwd),
                "session_dir": str(session_dir),
                "session_id": item_id,
                "agent_label": agent_label,
                "timeout_s": int(item.get("timeout_s") or 3600),
                "kill_check": kill_check,
                "model": item.get("model"),
            }
            if acp_matrix.acp_backend_name(backend_name) is not None:
                # ACP backends stream normalized events and relay permission
                # prompts through the hub while the turn runs.
                run_kwargs["permission_mode"] = item.get("permission_mode") or "supervised"
                run_kwargs["settings_source"] = item.get("settings_source")
                run_kwargs["on_event"] = self._event_poster(item_id)
                run_kwargs["permission_fn"] = self._permission_relay(item_id)
                run_kwargs["followup_fn"] = self._followup_fetcher(item_id)
                # Session-scoped hub MCP (task #1744), minted only if the agent
                # supports HTTP MCP; the value never touches this frame.
                run_kwargs["credential_fn"] = self._session_credential(item)
                run_kwargs["idle_s"] = acp_idle_s()
                # Only an interactive item waits for follow-ups when it has had
                # none; an older hub that sends no flag gets no idle wait.
                run_kwargs["hold_idle"] = bool(item.get("hold_for_followups"))
                if backend_name == "acp:hermes":
                    run_kwargs["hermes_profile"] = profile
            if backend_name == ACCEPTANCE_BACKEND:
                # Launch, progress and stop events prove the job ran (task #2112).
                run_kwargs["on_event"] = self._event_poster(item_id)
            if resume_id is not None:
                run_kwargs["resume_session_id"] = resume_id
            if planned is not None and (
                self._cwd_allowed(str(item.get("cwd") or "")) != cwd
                or self._cwd_allowed(str(cwd)) != cwd
            ):
                # Backend construction/credential setup can take time. Refuse
                # substitution of either the approved alias or resolved path
                # immediately before handing that resolved path to the backend.
                return self._refuse("cwd_realpath_changed")
            result = backend.run(**run_kwargs)
        except Exception as exc:  # noqa: BLE001 - reported in the receipt
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                self.running_item = None
                cancelled = item_id in self._cancelled
                self._cancelled.discard(item_id)
        backend_status = str(result.get("status") or "error")
        # A stop observed by the backend wins. A stop delivered only after the
        # backend finished does not rewrite completed work as killed.
        if (
            backend_status == "killed"
            or observed_cancel
            or backend_status != "success"
            and (cancelled or self.stop_event.is_set())
        ):
            status = "killed"
            reason = "cancelled_by_hub" if cancelled else "node_shutdown"
        elif backend_status == "success":
            status, reason = "finished", None
        else:
            status, reason = "failed", str(result.get("error") or backend_status)[:300]
        if resume_id is not None and status == "finished":
            # Only a run that actually finished wrote the session file: a run
            # that errored or was stopped may never have touched it, and must
            # not absorb a terminal's concurrent write as its own (#1741 review).
            self._record_fingerprint(backend_name, resume_id)
        return {
            "status": status,
            "reason": reason,
            "backend_status": backend_status,
            "text": str(result.get("text") or "")[:8000],
            "exit_code": result.get("exit_code"),
            "files_modified": list(result.get("files_modified") or [])[:200],
            # The ACP-negotiated session id (None for native backends); the hub
            # persists it on node_dispatch.session_id so a dispatch joins to
            # agent_sessions.external_session_id. A native resume (task #1741)
            # keeps the session it continued.
            "session_id": result.get("session_id") or resume_id,
            "started_at": started,
            "finished_at": _iso_now(self.clock),
            # A supervised command's log on this node (task #2413).
            "log_path": result.get("log_path"),
            # Snapshot at command completion; surviving descendants may later
            # hit the log cap without changing this receipt (task #2552).
            "truncated": bool(result.get("truncated")),
            "log_capture": result.get("log_capture"),
        }

    # -- loop --

    def _flush_receipts(self) -> None:
        """Deliver queued receipts in order. Resending is safe: the hub dedupes."""
        while self._pending_receipts:
            item_id, receipt = self._pending_receipts[0]
            try:
                result = self.post_receipt(item_id, receipt)
            except NodeRequestError as exc:
                # A permanent refusal (e.g. the item was reassigned after its
                # lease expired) will never succeed; drop it loudly. The hub
                # has answered for this attempt, so the outcome is settled.
                logger.error("hub refused receipt for %s: %s", item_id, exc)
            else:
                logger.info(
                    "receipt %s for %s%s",
                    receipt["status"],
                    item_id,
                    " (duplicate)" if result.get("duplicate") else "",
                )
            # The hub answered: the outcome is established even if the
            # receipt never reached the disk (task #2136).
            self._local_outcomes[item_id] = receipt.get("attempt")
            self._pending_receipts.pop(0)
            self._unspool(item_id)

    def run_once(self) -> str:
        """One iteration. Returns what happened, for tests and logs:
        ``registered`` | ``idle`` | ``ran`` | ``reconciled`` | ``held`` | ``backoff`` |
        ``auth_error`` | ``retired`` | ``agent_stopped`` | ``stopped``.

        ``agent_stopped``: the owner agent is paused or terminated; the node
        stays registered and re-asks after ``CLAIM_HOLD_S``.

        ``reconciled``: an outstanding claim operation was settled without
        running anything. ``held``: claiming is paused (see ``NodeClaimHeld``)."""
        if self.stop_event.is_set():
            return "stopped"
        try:
            self._flush_receipts()
            if not self.registered:
                self.register()
                self.backoff.reset()
                self._retired_announced = None
                try:
                    self.sync_config(force=True)
                except NodeRetiredError as exc:
                    # Retired between register and this sync (task #2194). The
                    # next iteration takes the retired path; say so now, not
                    # only at debug level.
                    logger.warning("config sync after register refused: %s", exc)
                except (NodeTransportError, NodeRequestError, NodeAuthError) as exc:
                    logger.debug("config sync after register deferred: %s", exc)
                return "registered"
            try:
                self.sync_config()
            except (NodeTransportError, NodeRequestError) as exc:
                logger.debug("config sync skipped: %s", exc)
            with self._claim_journal.locked() as journal:
                outcome = self._claim_and_run(journal)
            self._held_announced = None
            if outcome == "ran":
                self._flush_receipts()
            return outcome
        except (
            NodeClaimHeld,
            node_claim_journal.JournalBusy,
            node_claim_journal.JournalError,
        ) as exc:
            if self._held_announced != str(exc):
                self._held_announced = str(exc)
                logger.error("claiming held: %s", exc)
            self._sleep(CLAIM_HOLD_S)
            return "held"
        except NodeAgentStopped as exc:
            # Logged once per refusal text; an operator's resume lets the next
            # claim in, so neither the registration nor the backoff changes.
            if self._held_announced != str(exc):
                self._held_announced = str(exc)
                logger.warning("claiming paused by the hub: %s", exc)
            self._sleep(CLAIM_HOLD_S)
            return "agent_stopped"
        except NodeRetiredError as exc:
            # Logged once per refusal text, then quietly re-checked on a long
            # interval: an operator's node_unretire lets the next register in.
            if self._retired_announced != str(exc):
                self._retired_announced = str(exc)
                logger.error(
                    "this node was retired by an operator; it stays idle until an operator "
                    "runs node_unretire, or run it under a new node name (--name). Hub: %s",
                    exc,
                )
            self.registered = False
            self._sleep(RETIRED_BACKOFF_S)
            return "retired"
        except NodeAuthError as exc:
            logger.error("%s", exc)
            self.registered = False
            self._sleep(AUTH_BACKOFF_S)
            return "auth_error"
        except (NodeTransportError, NodeRequestError) as exc:
            delay = self.backoff.next_delay()
            logger.warning("hub call failed (%s); retrying in %.1fs", exc, delay)
            self._sleep(delay)
            return "backoff"

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.heartbeat_s):
            if not self.registered:
                continue
            try:
                self.heartbeat()
            except Exception as exc:  # noqa: BLE001 - the main loop owns reconnect
                logger.debug("heartbeat failed: %s", exc)

    # -- idle-session wakes (task #1873) --

    def _live_sessions_due(self) -> list[str] | None:
        """Native session ids with a pane on this host, at most once per ``LIVE_REPORT_S``.

        Keeps an idle granted session open on the hub (task #2428). Best effort:
        a pane listing that fails must never cost the wake poll itself.
        """
        if LIVE_SESSIONS_FEATURE not in self.hub_features:
            return None
        now = time.monotonic()
        if self._live_reported_at is not None and now - self._live_reported_at < LIVE_REPORT_S:
            return None
        lister = getattr(self._wake_transport, "live_sessions", None)
        if lister is None:
            return None
        self._live_reported_at = now
        try:
            ids = sorted(e for e in lister() if len(e) <= MAX_LIVE_ID_CHARS)
            return ids[:MAX_LIVE_REPORT]
        except Exception as exc:  # noqa: BLE001 - one bad listing must not stop the loop
            logger.debug("live session listing failed: %s", type(exc).__name__)
            return None

    def wake_once(self) -> int:
        """One wake long-poll: deliver each offer in its terminal, post its receipt.

        Returns how many offers were handled. Offers carry ids and an integer
        count only; anything malformed is reported failed and never typed.
        """
        if WAKE_FEATURE not in self.hub_features:
            return 0
        if not self._flush_wake_receipts():
            return 0
        if self._wake_transport is None:
            from .node_wake_transport import default_waker

            self._wake_transport = default_waker()
        poll: dict[str, Any] = {"wait_s": self.claim_wait_s, "offer_version": 3}
        live = self._live_sessions_due()
        if live:
            poll["live_sessions"] = live
        result = self._call("/api/node/wake", poll, timeout=self.claim_wait_s + 15)
        offers = result.get("wakes")
        if not isinstance(offers, list):
            return 0
        handled = 0
        for offer in offers[:50]:
            if not isinstance(offer, dict) or not isinstance(offer.get("session_id"), str):
                continue
            external = offer.get("external_session_id")
            unread = offer.get("unread")
            seq = offer.get("newest_seq")
            expires_at = offer.get("expires_at")
            offer_id = offer.get("offer_id")
            if not isinstance(seq, int) or isinstance(seq, bool):
                continue  # no seq to answer with; the hub's lease lapses and it re-offers
            if (
                not isinstance(external, str)
                or isinstance(unread, bool)
                or not isinstance(unread, int)
                or unread <= 0
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not 0 < expires_at < 253402300800
                or not isinstance(offer_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", offer_id)
            ):
                outcome, reason = "failed", "bad_offer"
            elif self.clock() >= expires_at:
                outcome, reason = "skipped", "offer_expired"
            elif seq <= self._wake_announced.get(offer["session_id"], -1):
                outcome, reason = "skipped", "already_delivered"
            else:
                try:
                    device_login._write_private(
                        self._wake_receipt_path(offer_id),
                        {
                            "session_id": offer["session_id"],
                            "seq": seq,
                            "offer_id": offer_id,
                            "expires_at": expires_at,
                            "outcome": "skipped",
                            "reason": "delivery_uncertain",
                            "attempting": True,
                        },
                    )

                    def authorize(offer=offer, seq=seq, expires_at=expires_at, offer_id=offer_id):
                        check = self._call(
                            "/api/node/wake/check",
                            {
                                "session_id": offer["session_id"],
                                "seq": seq,
                                "expires_at": expires_at,
                                "offer_id": offer_id,
                            },
                            timeout=min(15.0, max(0.01, expires_at - self.clock())),
                        )
                        return check.get("remaining_s", 0.0) if check.get("valid") is True else 0.0

                    outcome, reason = self._wake_transport.deliver(
                        external,
                        unread,
                        expires_at=expires_at,
                        authorize=authorize,
                    )
                except (NodeTransportError, NodeRequestError, NodeAuthError):
                    # The callback failed before a local prompt was submitted.
                    outcome, reason = "failed", "transport_error"
                except OSError:
                    # The durable intent write failed before invoking the terminal.
                    outcome, reason = "failed", "spool_unavailable"
                except Exception as exc:  # noqa: BLE001 - one bad wake must not stop the loop
                    logger.warning("wake delivery raised %s", type(exc).__name__)
                    outcome, reason = "skipped", "delivery_uncertain"
            body: dict[str, Any] = {
                "session_id": offer["session_id"],
                "seq": seq,
                "outcome": outcome,
            }
            if (
                isinstance(expires_at, (int, float))
                and not isinstance(expires_at, bool)
                and 0 < expires_at < 253402300800
            ):
                body["expires_at"] = expires_at
            if reason:
                body["reason"] = reason
            if isinstance(offer_id, str) and re.fullmatch(r"[a-f0-9]{32}", offer_id):
                body["offer_id"] = offer_id
                if outcome == "delivered" or reason == "delivery_uncertain":
                    self._wake_announced[offer["session_id"]] = max(
                        seq, self._wake_announced.get(offer["session_id"], 0)
                    )
                if outcome == "delivered":
                    body["delivered_at"] = self.clock()
                try:
                    device_login._write_private(self._wake_receipt_path(offer_id), body)
                except OSError as exc:
                    # If this was delivered the pre-prompt intent remains on
                    # disk, so restart recovery fails closed rather than retypes.
                    logger.warning("wake receipt spool failed: %s", type(exc).__name__)
                self._pending_wake_receipts.append(body)
                if not self._flush_wake_receipts():
                    return handled + 1
            handled += 1
        return handled

    def _wake_loop(self) -> None:
        backoff = Backoff()
        while not self.stop_event.is_set():
            if not self.registered or WAKE_FEATURE not in self.hub_features:
                self.stop_event.wait(self.heartbeat_s)
                continue
            try:
                self.wake_once()
                backoff.reset()
            except NodeAuthError as exc:
                logger.debug("wake poll refused: %s", exc)
                self.stop_event.wait(AUTH_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 - the main loop owns reconnect
                logger.debug("wake poll failed: %s", exc)
                self.stop_event.wait(backoff.next_delay())

    def start_wakes(self) -> bool:
        """Start the wake loop beside the heartbeat thread, only with ``UA_NODE_WAKE=1``."""
        if not config.node_wake_enabled() or self._wake_thread is not None:
            return self._wake_thread is not None
        self._wake_thread = threading.Thread(
            target=self._wake_loop, name="ua-node-wake", daemon=True
        )
        self._wake_thread.start()
        return True

    def start_heartbeats(self) -> None:
        if self._heartbeat_thread is None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="ua-node-heartbeat", daemon=True
            )
            self._heartbeat_thread.start()

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        self.start_heartbeats()
        if self.start_wakes():
            logger.info("idle-session wakes on (UA_NODE_WAKE=1)")
        while not self.stop_event.is_set():
            self.run_once()
        # A receipt produced by the shutdown kill still goes out, bounded.
        for _attempt in range(3):
            if not self._pending_receipts:
                break
            try:
                self._flush_receipts()
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not deliver receipt on shutdown: %s", exc)
                time.sleep(1)
        logger.info("node %s stopped", self.node)


def _default_backend_factory(name: str, agent_label: str | None = None) -> Any:
    """``acp:<harness>`` builds an AcpBackend; everything else goes through the
    executor registry. A failed construct surfaces as backend_unavailable in
    the receipt — there is no fallback to a different backend."""
    if acp_matrix.acp_backend_name(name) is not None:
        from .acp_backend import AcpBackend

        return AcpBackend(acp_matrix.acp_backend_name(name))
    from .executor import backends as backend_mod

    return backend_mod.get_backend(name, agent_label=agent_label)


def _iso_now(clock: Callable[[], float]) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="seconds")


def _saved_hub(token_path: Path | None) -> str:
    try:
        saved = json.loads((token_path or device_login.default_token_path()).read_text())
    except (OSError, ValueError):
        return ""
    return str(saved.get("hub") or "") if isinstance(saved, dict) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent node",
        description=(
            "Run this device as a ua-node: connect out to the hub, register host facts, "
            "heartbeat, and execute dispatched work through the local executor backends."
        ),
    )
    parser.add_argument(
        "--hub",
        default=os.environ.get("UA_HUB_URL", ""),
        help="hub base URL (default: $UA_HUB_URL, else the hub of the saved sign-in)",
    )
    parser.add_argument("--name", default=None, help="node name (default: $UA_NODE_NAME or host)")
    parser.add_argument(
        "--token-file", type=Path, default=None, help="saved sign-in (default: login's path)"
    )
    parser.add_argument(
        "--heartbeat-s", type=float, default=DEFAULT_HEARTBEAT_S, help="heartbeat interval"
    )
    parser.add_argument(
        "--claim-wait-s", type=float, default=DEFAULT_CLAIM_WAIT_S, help="long-poll wait"
    )
    parser.add_argument(
        "--once", action="store_true", help="register, claim at most one item, then exit"
    )
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="allow plain HTTP to a non-tailnet host (lab networks only)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    hub = args.hub or _saved_hub(args.token_file)
    if not hub:
        parser.error("--hub is required (or set UA_HUB_URL, or run `unified-agent login` first)")
    try:
        transport = HubTransport(
            hub, token_path=args.token_file, allow_insecure=args.allow_insecure
        )
    except NodeAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    agent = NodeAgent(
        node=args.name or default_node_name(),
        transport=transport,
        heartbeat_s=args.heartbeat_s,
        claim_wait_s=args.claim_wait_s,
    )
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    logger.info("ua-node %s → %s (backends allowed: %s)", agent.node, transport.hub, agent.backends)
    if args.once:
        outcome = agent.run_once()
        if outcome == "registered":
            outcome = agent.run_once()
        agent._flush_receipts()
        return 0 if outcome in ("idle", "ran", "reconciled") else 1
    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
