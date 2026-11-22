"""Sandboxed file tools.

Read and write reach are deliberately asymmetric (task #497, option C of #495):
reads resolve inside the workspace OR any project checkout, writes only inside
the workspace. A relative path still means "relative to the workspace root" for
both, so reaching a project repo is an explicit act requiring an absolute path.
"""

import hashlib
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import agent_access, config, managed_secrets, trust_presets
from ..safety import (
    guard_hook_dir,
    guard_project_read,
    guard_secret_filename,
    project_read_roots,
    resolve_for_project_read,
    resolve_in_workspace,
)


def _resolve_read(path: str):
    """Resolve a read path without letting ``..`` opt into project-root reach."""
    candidate = Path(path)
    if agent_access.managed_secret_mode():
        secret_root = managed_secrets._root()
        resolved = (
            candidate if candidate.is_absolute() else config.workspace_root() / candidate
        ).resolve()
        if resolved == secret_root or resolved.is_relative_to(secret_root):
            raise ToolError("managed secret storage is write-only")
    if not candidate.is_absolute() and ".." in candidate.parts:
        # Preserve the old relative-path contract: dot-dot is fine only while it
        # remains inside the workspace. A relative escape must fail here rather
        # than being reclassified as an explicit cross-project read.
        # This branch does not go through resolve_for_project_read, so the
        # read-only secret-name guard has to be applied here too.
        resolved = resolve_in_workspace(path)
        guard_secret_filename(resolved, path)
        return resolved
    return resolve_for_project_read(path)


def _read_text(p) -> str:
    if p.stat().st_size > config.READ_MAX_BYTES:
        raise ToolError(
            f"file is larger than {config.READ_MAX_BYTES} bytes; use fs_read with offset/limit "
            "or fs_search to inspect parts of it"
        )
    try:
        return p.read_text(errors="replace")
    except IsADirectoryError as e:
        raise ToolError(f"{p} is a directory; use fs_list") from e


def fs_read(path: str, offset: int = 0, limit: int = 2000) -> dict:
    """Read a text file from the workspace (max 2000 lines per call; page with offset).

    Returns {path, content, total_lines, truncated}.
    """
    agent_access.enforce("fs_read", read_only=True)
    p = _resolve_read(path)
    if not p.exists():
        raise ToolError(f"no such file: {path}")
    lines = _read_text(p).splitlines()
    window = lines[offset : offset + max(1, min(limit, 5000))]
    return {
        "path": path,
        "content": "\n".join(window),
        "total_lines": len(lines),
        "truncated": offset + len(window) < len(lines),
    }


def _refuse_imessage_bridge_write(path: Path) -> None:
    """Refuse writes inside the resolved iMessage bridge tree.

    ``notify`` executes ``send.ts`` with Spectrum credentials. On a
    single-workspace hub that tree lives inside the same root ``fs_write``
    can modify. The denylist is the resolved bridge directory
    (``UA_BRIDGE_DIR``, else ``process_workspace_root()/projects/imessage-bridge``).
    This is the only write helper in this module.
    """
    from .notify import _bridge_dir

    bridge = _bridge_dir()
    try:
        resolved = path.resolve()
        root = bridge.resolve()
    except OSError as e:
        raise ToolError(f"cannot resolve iMessage bridge path: {e}") from e
    if resolved == root or resolved.is_relative_to(root):
        raise ToolError(
            f"refusing to write {resolved} inside the iMessage bridge directory {root}. "
            "That tree supplies send.ts, which notify executes with Spectrum credentials. "
            "Set UA_BRIDGE_DIR if the bridge should live outside the workspace."
        )


_WRITE_LOCK = threading.RLock()


def fs_write(
    path: str, content: str, append: bool = False, expected_content_hash: str | None = None
) -> dict:
    """Write (or append to) a text file in the workspace. Max 2 MB per call.

    Creates parent directories as needed. Optional expected_content_hash binds
    replacement to existing bytes (empty string requires absence). Returns {path, bytes_written}.
    """
    agent_access.enforce("fs_write", read_only=False)
    data = content.encode()
    if len(data) > config.WRITE_MAX_BYTES:
        raise ToolError(f"content exceeds {config.WRITE_MAX_BYTES} bytes; write in chunks")
    p = resolve_in_workspace(path, protect_configured_hooks=True)
    _refuse_imessage_bridge_write(p)

    from .approvals import check_approval_gate, mark_governed_execution

    # Omitted preserves legacy unconditional writes and their approval payload.
    # An empty string explicitly attests absence; a digest attests existing bytes.
    if expected_content_hash is not None:
        if append:
            raise ToolError("expected_content_hash is only supported for replacement writes")
        if not isinstance(expected_content_hash, str) or (
            expected_content_hash != ""
            and re.fullmatch(r"[0-9a-f]{64}", expected_content_hash) is None
        ):
            raise ToolError("expected_content_hash must be a SHA-256 hash or empty for absence")

    def _compare() -> None:
        if expected_content_hash is None:
            return
        if p.is_symlink():
            raise ToolError("conditional writes refuse symlink destinations")
        if p.exists():
            if not p.is_file() or p.stat().st_size > config.WRITE_MAX_BYTES:
                raise ToolError("existing file cannot be compared within the write limit")
            observed = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            observed = ""
        if observed != expected_content_hash:
            raise ToolError("existing file changed; expected_content_hash does not match")

    payload = {
        "path": path,
        "append": append,
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }
    if expected_content_hash is not None:
        payload["expected_content_sha256"] = expected_content_hash
    with _WRITE_LOCK:
        _compare()
        check_approval_gate("fs_write", payload, requested_by=config.verified_agent_identity())
        # Bind the approval to the bytes it was reviewed against, including
        # absence. Recheck inside the actual filesystem writer, after the gate.
        if resolve_in_workspace(path, protect_configured_hooks=True) != p:
            raise ToolError("write destination changed during approval")
        _compare()
        mark_governed_execution()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab" if append else "wb") as f:
            f.write(data)
    return {"path": path, "bytes_written": len(data)}


def _write_skill_package(
    path: str, members: dict, *, expected_hash: str, content_hash: str
) -> dict:
    """Internal catalog writer: one governed directory swap, never a bulk MCP tool.

    Every member receives fs_write path/policy checks. Approval binds the full
    manifest and the exact destination tree. A failed activation restores the
    prior directory; temporary payloads never become an installed partial tree.
    """
    from .. import safety, skills_registry
    from .approvals import check_approval_gate, mark_governed_execution

    agent_access.enforce("fs_write", read_only=False)
    target = Path(path)
    manifest = skills_registry._package_manifest(members)
    if skills_registry._package_hash(manifest) != content_hash:
        raise ToolError("skill package hash does not match its manifest")
    size = sum(len(data) for data, _ in members.values())
    if size > config.WRITE_MAX_BYTES:
        raise ToolError("skill package exceeds the filesystem write limit")

    def guard(candidate: Path) -> Path:
        for ancestor in (candidate, *candidate.parents):
            if ancestor.is_symlink():
                raise ToolError("skill installation refuses symlink destinations")
        resolved = resolve_in_workspace(str(candidate), protect_configured_hooks=True)
        if resolved != candidate:
            raise ToolError("skill destination changed during approval")
        _refuse_imessage_bridge_write(resolved)
        return resolved

    def snapshot() -> tuple[dict, set]:
        guard(target)
        if not target.exists():
            return {}, set()
        directories = set()
        existing = skills_registry._capture_package(
            target, "installation comparison", path_guard=guard, directory_paths=directories
        )
        return existing, directories

    def compare() -> str:
        existing, _ = snapshot()
        return (
            skills_registry._package_hash(skills_registry._package_manifest(existing))
            if target.exists()
            else ""
        )

    with _WRITE_LOCK:
        previous, old_directories = snapshot()
        removed = sorted(set(previous) - set(members))
        payload = {
            "path": str(target / "SKILL.md"),
            "append": False,
            "content_sha256": content_hash,
            "bytes": size,
            "expected_content_sha256": expected_hash,
            "package_manifest": manifest,
            "removed_paths": removed,
        }

        created_ancestors = set()
        parent = target.parent
        while not parent.exists():
            created_ancestors.add(parent)
            parent = parent.parent

        def policy_members() -> None:
            # Directory replacement also removes old members; protect removals
            # and repeat these checks after approval/staging, before mutation.
            destinations = {target / member: member for member in set(members) | set(previous)}
            for member in old_directories:
                destinations[target / member] = member
            # Every directory installed by the staged tree is a write target,
            # including ancestors that mkdir will create outside the package.
            destinations[target] = None
            for member in members:
                for parent in (target / member).parents:
                    if parent == target:
                        break
                    destinations[parent] = parent.relative_to(target).as_posix()
            for parent in created_ancestors:
                destinations[parent] = None
            for candidate, member in sorted(destinations.items()):
                if member is not None:
                    skills_registry._package_path(member)
                destination = guard(candidate)
                data = members.get(member, (b"", 0o644))[0]
                safety.check_policy_gate(
                    "fs_write",
                    {
                        "path": str(destination),
                        "append": False,
                        "content": data.decode("utf-8", errors="replace"),
                        "content_sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                        "package_removal": member in removed or member in old_directories,
                    },
                    originator=config.verified_agent_identity(),
                )

        policy_members()
        if compare() != expected_hash:
            raise ToolError("existing skill changed; expected_hash does not match its package")
        check_approval_gate("fs_write", payload, requested_by=config.verified_agent_identity())
        if compare() != expected_hash:
            raise ToolError("existing skill changed during approval; retry with its current hash")
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".ua-skill-stage-", dir=target.parent))
        backup = None
        activated = False
        try:
            for member, (data, mode) in members.items():
                destination = stage / member
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                destination.chmod(mode)
            # Revalidate source bytes, scoped destination, and full base right
            # before the first mutation. Staging has not spent any receipts.
            if compare() != expected_hash:
                raise ToolError("existing skill changed during staging")
            staged = skills_registry._capture_package(stage, "staged package verification")
            if skills_registry._package_manifest(staged) != manifest:
                raise ToolError("staged skill package changed before activation")
            policy_members()
            guard(target)
            if target.exists():
                backup = Path(tempfile.mkdtemp(prefix=".ua-skill-backup-", dir=target.parent))
                backup.rmdir()
                mark_governed_execution()
                os.replace(target, backup)
            else:
                mark_governed_execution()
            os.replace(stage, target)
            activated = True
        except BaseException:
            if backup is not None and backup.exists():
                os.replace(backup, target)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if activated and backup is not None and backup.exists():
                shutil.rmtree(backup)
    return {"path": str(target / "SKILL.md"), "bytes_written": size}


def _guarded_read_entry(f: Path, readable_roots: list[Path]) -> tuple[Path, Path] | None:
    """(real path, containing root) for an enumerated entry fs_read would allow, else None.

    Recursive enumeration must not become a bypass around the same
    project-read guards used by fs_read. Re-deriving each candidate's real
    (symlink-resolved) location and matching it against the readable roots
    closes denied directories/secret filenames and rejects symlinks that escape
    all readable roots -- without calling resolve_for_project_read (and so
    re-resolving the active worktree, a DB round trip) once per candidate. The
    containing root is looked up fresh per entry rather than assumed to be the
    search root's, so a symlink into a *different* readable root reports
    correctly instead of crashing relative_to() against the wrong one.
    """
    try:
        real = f.resolve()
    except OSError:
        return None
    containing_root = next((r for r in readable_roots if real == r or real.is_relative_to(r)), None)
    if containing_root is None:
        return None  # symlink or glob escapes every readable root
    try:
        # Hook-dir exclusion applies to every readable root, including the
        # workspace: resolve_in_workspace already enforces it there, and a
        # listing must not become a way to read what a direct fs_read on the
        # same path would refuse. Secret filenames are the same.
        guard_hook_dir(real, containing_root, str(f))
        if containing_root != readable_roots[0]:
            guard_project_read(real, containing_root, str(f))
        else:
            guard_secret_filename(real, str(f))
    except ToolError:
        return None
    return real, containing_root


def fs_list(path: str = ".", glob: str | None = None, max_entries: int = 500) -> dict:
    """List a workspace directory (optionally filtered by a glob like '*.py').

    Returns {entries: [{name, type, size}], truncated}.
    """
    agent_access.enforce("fs_list", read_only=True)
    from ..hosted_capabilities import refuse_resolved_empty_workspace

    if glob and (Path(glob).is_absolute() or ".." in Path(glob).parts):
        raise ToolError(f"glob {glob!r} must be relative to path and must not contain '..'")
    p = _resolve_read(path)
    refuse_resolved_empty_workspace("fs_list", p)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    cap = max(1, min(max_entries, 2000))
    items = sorted(p.glob(glob) if glob else p.iterdir(), key=lambda x: x.name)
    readable_roots = project_read_roots()
    entries = []
    for entry in items:
        guarded = _guarded_read_entry(entry, readable_roots)
        if guarded is None:
            continue
        real = guarded[0]
        try:
            trust_presets.check_path(real)
        except ToolError:
            continue
        entries.append(
            {
                "name": entry.name,
                "type": "dir" if real.is_dir() else "file",
                "size": real.stat().st_size if real.is_file() else None,
            }
        )
        if len(entries) > cap:
            break
    return {"entries": entries[:cap], "truncated": len(entries) > cap}


def fs_search(
    pattern: str, path: str = ".", glob: str | None = None, max_results: int = 100
) -> dict:
    """Regex-search text files under a workspace directory (skips binaries and files >1 MB).

    Returns {matches: [{path, line_no, line}], truncated}.
    """
    agent_access.enforce("fs_search", read_only=True)
    from ..hosted_capabilities import refuse_resolved_empty_workspace

    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}") from e
    root = _resolve_read(path)
    refuse_resolved_empty_workspace("fs_search", root)
    if not root.is_dir():
        raise ToolError(f"not a directory: {path}")
    readable_roots = project_read_roots()
    cap = max(1, min(max_results, 1000))
    matches, truncated = [], False
    for f in sorted(root.rglob(glob or "*")):
        guarded = _guarded_read_entry(f, readable_roots)
        if guarded is None:
            continue
        real, containing_root = guarded
        try:
            if not real.is_file() or real.stat().st_size > config.SEARCH_FILE_MAX_BYTES:
                continue
            # Read the already-guarded `real` path, not `f`: if f is a
            # symlink, re-reading through f would re-follow it and could
            # pick up a different target than the one just validated, if it
            # was retargeted between the resolve() above and this read.
            text = real.read_bytes()
            if b"\x00" in text[:8192]:
                continue
            for i, line in enumerate(text.decode(errors="replace").splitlines(), 1):
                if rx.search(line):
                    if len(matches) >= cap:
                        truncated = True
                        break
                    matches.append(
                        {
                            "path": str(real.relative_to(containing_root)),
                            "line_no": i,
                            "line": line[:500],
                        }
                    )
        except OSError:
            continue
        if truncated:
            break
    return {"matches": matches, "truncated": truncated}
