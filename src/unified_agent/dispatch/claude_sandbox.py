"""Fail-closed Claude sandbox settings for unattended approved dispatch.

Global ``UA_CLAUDE_SKIP_PERMISSIONS=1`` does not by itself disable the
permission layer. ``dispatch.harness_sandbox`` attaches
``--dangerously-skip-permissions`` only after it confirms a per-run settings
file whose sandbox is forced on (``failIfUnavailable``,
``allowUnsandboxedCommands: false``) and the host sandbox probe passes.
``CliProcessSpawner`` writes that file for approved dispatch. The executor
writes the same file when the env knob is set, and still refuses the launch
when the file or the probe does not confirm the sandbox.

Claude's sandbox applies to the Bash tool; MCP/Read/Write are gated by the
permission layer. Skip-permissions without a fail-closed sandbox is the
unsafe combination we refuse; sandbox without skip-permissions is what left
run-622 unable to self-report.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import config

SIDECAR_SUFFIX = ".ua-dispatch.json"
SETTINGS_SUFFIX = ".claude-settings.json"

# Domains the sandboxed Bash tool may reach. Hub MCP itself is not Bash —
# Claude talks to the hub over its configured MCP transport — but any
# curl/fetch from Bash still needs an allowlist when network is strict.
_DEFAULT_NETWORK_DOMAINS = (
    "api.anthropic.com",
    "*.anthropic.com",
    "statsig.anthropic.com",
    "sentry.io",
)


def _network_domains() -> list[str]:
    raw = (os.environ.get("UA_DISPATCH_CLAUDE_NETWORK") or "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    domains = list(_DEFAULT_NETWORK_DOMAINS)
    # Tailscale Mac hub IP is the usual desktop→hub path; keep configurable.
    hub = (os.environ.get("UA_DISPATCH_HUB_HOST") or "").strip()
    if hub:
        domains.append(hub)
    return domains


def _filesystem_permission_denies() -> list[str]:
    """Read/Edit deny rules mirroring sandbox filesystem blocks.

    Bash is sandboxed; Read/Write/Edit use the permission layer. Skip-permissions
    still honors deny rules, so these paths stay blocked when dispatch attaches
    --dangerously-skip-permissions alongside this settings file.
    """
    return [
        "WebSearch",
        "WebFetch",
        # Mirror sandbox.filesystem denyRead / denyWrite (~/ is home-relative).
        "Read(~/.ssh/**)",
        "Edit(~/.ssh/**)",
        "Read(~/.unified_agent/service.env)",
        "Read(~/.unified_agent/**)",
        "Edit(~/.unified_agent/**)",
        "Read(~/.aws/**)",
        "Edit(~/.aws/**)",
        "Read(~/.gnupg/**)",
        "Edit(~/.gnupg/**)",
        "Read(~/Library/Keychains/**)",
        "Edit(~/Library/Keychains/**)",
        "Read(~/vaults/**)",
        "Edit(~/vaults/**)",
        "Edit(~/Library/Application Support/Google/Chrome/**)",
        "Edit(//var/run/docker.sock)",
    ]


def build_claude_dispatch_settings(*, cwd: str) -> dict[str, Any]:
    """Settings JSON body for a fail-closed dispatch Claude session."""
    write_root = str(Path(cwd).expanduser().resolve())
    home = str(Path.home())
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "excludedCommands": [],
            "network": {
                "allowedDomains": _network_domains(),
                "allowUnixSockets": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
            "filesystem": {
                "allowWrite": [write_root],
                "denyWrite": [
                    f"{home}/.ssh",
                    f"{home}/.unified_agent",
                    f"{home}/vaults",
                    f"{home}/.aws",
                    f"{home}/.gnupg",
                    f"{home}/Library/Keychains",
                    f"{home}/Library/Application Support/Google/Chrome",
                    "/var/run/docker.sock",
                ],
                "denyRead": [
                    f"{home}/.ssh",
                    f"{home}/.unified_agent/service.env",
                    f"{home}/.aws",
                    f"{home}/.gnupg",
                    f"{home}/Library/Keychains",
                ],
            },
        },
        "permissions": {
            # Do NOT set disableBypassPermissionsMode — dispatch attaches
            # --dangerously-skip-permissions only with this fail-closed file.
            "deny": _filesystem_permission_denies(),
        },
    }


def _dispatch_runs_path() -> Path:
    """Where per-run artifacts live, without creating the directory."""
    return config.data_home() / "dispatch-runs"


def dispatch_runs_dir() -> Path:
    """Per-run settings and logs, private to the hub user."""
    path = _dispatch_runs_path()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


# --- retention (task #2191) ---------------------------------------------------
#
# Every opted-in Claude dispatch run writes a settings file and a sidecar here,
# including runs the host probe refuses. Both are launch inputs: the CLI reads
# its settings at start, and the child reads the sidecar while it runs. Neither
# is evidence, so both are removed when the run ends (the detached supervisor
# after the child exits, the spawner when the launch is refused, the executor
# backend after its synchronous run). An age sweep in queue hygiene collects
# any the end-of-run path missed (a killed supervisor, a hub restart).
#
# Launch logs (``<run_id>.log``) are evidence a run can cite, and nothing here
# deletes them.
#
# Deletion is by identity, never by name alone. ``dispatch_resume`` reuses a
# run_id, so a lingering child's supervisor must not delete the files a resumed
# launch wrote under the same names. Writers therefore replace the entry
# atomically (a fresh inode every write), every deleter snapshots the identity
# (device, inode, mtime_ns, ctime_ns) of what it owns, and an entry is unlinked
# only while its ``lstat`` still matches that snapshot.
#
# Removal only ever unlinks a directory entry inside ``dispatch-runs``: a
# symlinked ``dispatch-runs`` is refused, entries are inspected with ``lstat``
# (a symlink entry is unlinked as a link; its target is never touched), and
# directories are left alone.

#: The per-run artifacts removed at run end and by the age sweep.
RUN_ARTIFACT_SUFFIXES = (SETTINGS_SUFFIX, SIDECAR_SUFFIX)

#: ``(st_dev, st_ino, st_mtime_ns, st_ctime_ns)`` of one directory entry.
ArtifactIdentity = tuple[int, int, int, int]


def _write_fresh(path: Path, text: str) -> None:
    """Write ``path`` as a new inode (temp file + atomic replace).

    A rewrite under the same name (a resumed run) therefore never keeps the
    identity an earlier owner snapshotted, so that owner's cleanup skips it.
    ``os.replace`` swaps the directory entry; it never writes through a symlink.

    On failure this call removes what it created (task #2196): the temp file,
    and the final entry too if the error surfaced after the rename landed it
    (a network filesystem can report a completed rename as failed). The
    caller never learns that path, so nothing else would remove it before
    the age sweep. The final entry is unlinked only while it is still the
    inode written here (device, inode and mtime; a rename changes ctime),
    and then through ``_unlink_if_same`` with its full identity, so a file
    another writer put under the same name in between is kept.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    written: tuple[int, int, int] | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            st = os.fstat(handle.fileno())
        # Ownership: (dev, ino, mtime_ns) names this inode wherever it is
        # linked. ctime cannot: a rename updates it, so a pre-rename ctime
        # never matches the landed file.
        written = (st.st_dev, st.st_ino, st.st_mtime_ns)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if written is not None:
            try:
                landed = os.lstat(path)
                if (landed.st_dev, landed.st_ino, landed.st_mtime_ns) == written:
                    # The same identity-checked unlink every deleter uses:
                    # full ``_identity`` (ctime included) re-read just before it.
                    _unlink_if_same(path, _identity(landed))
            except OSError:
                pass
        raise


def _identity(st: os.stat_result) -> ArtifactIdentity:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def artifact_identity(path: Path) -> ArtifactIdentity | None:
    """The ``lstat`` identity of a non-directory entry, else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return None
    return _identity(st)


def _dispatch_runs_root() -> Path | None:
    """``dispatch-runs`` when it is a real directory, else None (absent or a symlink)."""
    path = _dispatch_runs_path()
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        # A symlink (or anything else) at this path is not ours to clean through.
        return None
    return path


def _orphan_temp_run_id(name: str) -> str | None:
    """The run_id of an ``_write_fresh`` temp file name, else None (task #2194).

    ``tempfile.mkstemp`` names it ``.<run_id><suffix>.<random>.tmp``. A crash
    between ``mkstemp`` and ``os.replace`` leaves it behind; nothing else
    knows its name, so only the age sweep can collect it.
    """
    if not (name.startswith(".") and name.endswith(".tmp")):
        return None
    stem, _sep, _rand = name[1 : -len(".tmp")].rpartition(".")
    for suffix in RUN_ARTIFACT_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)]
    return None


def _is_artifact(path: Path, root: Path) -> bool:
    return path.parent == root and path.name.endswith(RUN_ARTIFACT_SUFFIXES)


def _unlink_if_same(path: Path, expected: ArtifactIdentity) -> bool:
    """Unlink one entry only while it is still the one ``expected`` names."""
    if artifact_identity(path) != expected:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def run_artifact_paths(run_id: str) -> list[Path]:
    """The settings file and sidecar a run with ``run_id`` may have written."""
    rid = str(run_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid in {".", ".."}:
        return []
    root = _dispatch_runs_path()
    return [root / f"{rid}{suffix}" for suffix in RUN_ARTIFACT_SUFFIXES]


def snapshot_artifacts(paths: list[Path] | tuple[Path, ...]) -> dict[Path, ArtifactIdentity]:
    """Identity of each existing per-run artifact among ``paths``. Never raises.

    Only direct children of a real ``dispatch-runs`` with an artifact suffix
    are recorded, so the snapshot can never authorize a delete elsewhere.
    """
    root = _dispatch_runs_root()
    if root is None:
        return {}
    out: dict[Path, ArtifactIdentity] = {}
    for raw in paths:
        path = Path(raw)
        if not _is_artifact(path, root):
            continue
        ident = artifact_identity(path)
        if ident is not None:
            out[path] = ident
    return out


def snapshot_run_artifacts(run_id: str) -> dict[Path, ArtifactIdentity]:
    """Snapshot the settings file and sidecar currently written for ``run_id``."""
    return snapshot_artifacts(run_artifact_paths(run_id))


def discard_run_artifacts(snapshot: Mapping[Path, ArtifactIdentity]) -> list[str]:
    """Remove the snapshotted artifacts that are still the same entries.

    An entry rewritten since the snapshot (a resumed run reusing the run_id)
    no longer matches and is kept. Never raises; returns the names removed.
    """
    root = _dispatch_runs_root()
    if root is None:
        return []
    removed: list[str] = []
    for path, expected in snapshot.items():
        path = Path(path)
        if not _is_artifact(path, root):
            continue
        try:
            if _unlink_if_same(path, expected):
                removed.append(path.name)
        except OSError:
            continue
    return removed


def expire_stale_run_artifacts(
    *,
    max_age_days: float | None = None,
    live_run_ids: frozenset[str] | set[str] = frozenset(),
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete settings files and sidecars older than the retention window.

    Temp files a crashed ``_write_fresh`` left (``.<run_id><suffix>.<rand>.tmp``,
    task #2194) expire on the same terms; only regular files are removed.

    ``max_age_days`` defaults to ``config.dispatch_settings_retention_days()``;
    0 keeps everything. Age is the entry's own mtime (``lstat``). Artifacts of
    a run in ``live_run_ids`` are kept whatever their age, and an entry is
    unlinked only if it is still the one that was found stale (a resume that
    rewrote it in between keeps it). Launch logs are never touched. Never
    raises; an unreadable directory reports ``error``.
    """
    days = config.dispatch_settings_retention_days() if max_age_days is None else max_age_days
    out: dict[str, Any] = {"retention_days": days, "expired": [], "dry_run": dry_run}
    if days <= 0:
        return out
    root = _dispatch_runs_root()
    if root is None:
        return out
    cutoff = (time.time() if now is None else now) - days * 86400.0
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    expired: list[str] = []
    for entry in entries:
        name = entry.name
        suffix = next((s for s in RUN_ARTIFACT_SUFFIXES if name.endswith(s)), None)
        orphan_run_id = _orphan_temp_run_id(name) if suffix is None else None
        if suffix is None and orphan_run_id is None:
            continue
        run_id = name[: -len(suffix)] if suffix is not None else orphan_run_id
        if run_id in live_run_ids:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode) or st.st_mtime >= cutoff:
                continue
            if orphan_run_id is not None and not stat.S_ISREG(st.st_mode):
                # mkstemp makes a regular file; anything else under that
                # name is not one of ours.
                continue
            if dry_run or _unlink_if_same(root / name, _identity(st)):
                expired.append(name)
        except OSError:
            continue
    out["expired"] = sorted(expired)
    return out


def write_claude_dispatch_settings(run_id: str, *, cwd: str) -> Path:
    """Persist per-run settings under the hub data home; return the path."""
    settings_dir = dispatch_runs_dir()
    path = settings_dir / f"{run_id}{SETTINGS_SUFFIX}"
    _write_fresh(
        path,
        json.dumps(build_claude_dispatch_settings(cwd=cwd), indent=2, sort_keys=True) + "\n",
    )
    return path


def write_dispatch_sidecar(
    run_id: str,
    *,
    task_id: int | str,
    agent_label: str,
) -> Path:
    """Write per-run identity metadata under the hub data home.

    Stored outside the checkout so concurrent dispatches sharing a cwd cannot
    overwrite each other's sidecar and the checkout stays clean.
    """
    path = dispatch_runs_dir() / f"{run_id}{SIDECAR_SUFFIX}"
    payload = {
        "run_id": run_id,
        "task_id": int(task_id) if str(task_id).isdigit() else task_id,
        "agent_label": agent_label,
        "report_tool": "dispatch_report_self",
    }
    _write_fresh(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def append_dispatch_report_instructions(
    task_spec: str,
    *,
    run_id: str,
    agent_label: str,
    sidecar_path: str | Path,
) -> str:
    """Ensure the prompt tells the child how to self-report via the hub tool."""
    sidecar_ref = str(sidecar_path)
    block = (
        "\n\n---\n"
        "## Approved-dispatch completion (required)\n"
        f"- Your dispatch run_id is `{run_id}` and agent label is `{agent_label}`.\n"
        f"- Identity is also in `{sidecar_ref}` (Read that file — not in this checkout).\n"
        "- Do **not** dump the process environment (`env`, `printenv`, `env | grep`). "
        "Use the run_id / agent label above or the sidecar file.\n"
        "- When the work is done, call the hub MCP tool `dispatch_report_self` with "
        f'`state="finished"`, `run_id="{run_id}"`, and a non-empty `evidence` '
        "(commit SHA, PR URL, or log path you authored). If MCP is unavailable, run "
        f"`unified-agent-dispatch-scheduler report finished --run-id {run_id} "
        "--evidence <ref>` instead.\n"
        '- On failure, call `dispatch_report_self` with `state="failed"`, the same '
        "`run_id`, and `failure_reason`.\n"
        "- Do not claim success without that report; exit 0 alone is not evidence.\n"
    )
    base = (task_spec or "").rstrip()
    if "dispatch_report_self" in base and run_id in base:
        return base
    return base + block
