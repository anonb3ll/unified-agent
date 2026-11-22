"""Versioned and immutable release artifact manager with rollback support.

Enforces an immutable deployment boundary for unified-agent and imessage-bridge
by creating versioned snapshot packages with MANIFEST.json sha256 checksums,
read-only file permissions, atomic symlink cutover, and rollback proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

EXCLUDE_PATTERNS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".worktrees",
    "*.pyc",
    "*.log",
    "*.tmp",
    ".DS_Store",
}


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _validate_name_component(name: str, label: str) -> None:
    """Validate that component or version is a safe single path component."""
    if not name or not isinstance(name, str) or not _SAFE_NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"Invalid {label} '{name}': must be a safe single path component matching ^[a-zA-Z0-9_.-]+$"
        )


def _get_git_commit_sha(repo_dir: Path) -> str:
    """Best-effort retrieval of current git commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def _sha256_file(path: Path) -> str:
    """Compute sha256 checksum of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_release(
    source_dir: Path,
    releases_base_dir: Path | None = None,
    component: str = "unified-agent",
    version: str = "v1.0.0",
    creator: str = "Desktop_agent3",
    dry_run: bool = False,
    update_current: bool = True,
    authorize: bool = False,
) -> dict[str, Any]:
    """Create an immutable release snapshot directory with MANIFEST.json."""
    _validate_name_component(component, "component")
    _validate_name_component(version, "version")

    if releases_base_dir is None:
        releases_base_dir = config.data_home() / "releases"

    comp_dir = releases_base_dir / component
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    commit_sha = _get_git_commit_sha(source_dir)
    short_sha = commit_sha[:7] if commit_sha != "unknown" else "nocommit"
    release_id = f"{timestamp}_{version}_{short_sha}"
    target_release_dir = comp_dir / release_id

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "component": component,
            "version": version,
            "release_id": release_id,
            "release_path": str(target_release_dir),
            "commit_sha": commit_sha,
            "updated_current": False,
        }

    comp_dir.mkdir(parents=True, exist_ok=True)
    if target_release_dir.exists():
        raise FileExistsError(f"Release directory {target_release_dir} already exists.")

    def ignore_func(dir_path: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            if name in EXCLUDE_PATTERNS:
                ignored.add(name)
            elif name.endswith(".pyc") or name.endswith(".log") or name.endswith(".tmp"):
                ignored.add(name)
        return ignored

    shutil.copytree(source_dir, target_release_dir, ignore=ignore_func, symlinks=False)

    manifest_files: dict[str, str] = {}
    for p in sorted(target_release_dir.rglob("*")):
        if p.is_file():
            rel_path = p.relative_to(target_release_dir).as_posix()
            manifest_files[rel_path] = _sha256_file(p)

    manifest_data = {
        "component": component,
        "version": version,
        "release_id": release_id,
        "created_at": timestamp,
        "creator": creator,
        "source_commit": commit_sha,
        "file_count": len(manifest_files),
        "files": manifest_files,
    }

    manifest_path = target_release_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Mark files read-only for immutability
    for p in target_release_dir.rglob("*"):
        if p.is_file():
            p.chmod(0o444)
        elif p.is_dir():
            p.chmod(0o555)

    updated_current = False
    current_symlink = comp_dir / "current"

    if update_current:
        if not authorize:
            pass
        else:
            tmp_symlink = comp_dir / f"current_tmp_{timestamp}"
            if tmp_symlink.is_symlink() or tmp_symlink.exists():
                tmp_symlink.unlink()
            tmp_symlink.symlink_to(release_id)
            tmp_symlink.replace(current_symlink)
            updated_current = True

    return {
        "ok": True,
        "dry_run": False,
        "component": component,
        "version": version,
        "release_id": release_id,
        "release_path": str(target_release_dir),
        "commit_sha": commit_sha,
        "file_count": len(manifest_files),
        "updated_current": updated_current,
    }


def verify_release(release_dir: Path) -> dict[str, Any]:
    """Verify integrity of a release artifact against its MANIFEST.json."""
    manifest_path = release_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": f"Missing MANIFEST.json in {release_dir}"}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Invalid MANIFEST.json: {exc}"}

    if not isinstance(manifest, dict):
        return {"ok": False, "error": f"Malformed MANIFEST.json structure in {release_dir}"}

    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        return {
            "ok": False,
            "error": f"Malformed MANIFEST.json structure in {release_dir}: 'files' field must be a mapping",
        }

    errors: list[str] = []

    # 1. Check all expected files exist, match sha256, and have safe paths
    for rel_path, expected_hash in expected_files.items():
        if rel_path.startswith("/") or ".." in rel_path.split("/"):
            errors.append(f"Unsafe path in manifest: {rel_path}")
            continue

        file_path = (release_dir / rel_path).resolve()
        try:
            file_path.relative_to(release_dir.resolve())
        except ValueError:
            errors.append(f"Manifest path escapes release directory: {rel_path}")
            continue

        if not file_path.is_file():
            errors.append(f"Missing file: {rel_path}")
            continue

        actual_hash = _sha256_file(file_path)
        if actual_hash != expected_hash:
            errors.append(
                f"Checksum mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
            )

    # 2. Reject unexpected files in release tree (except MANIFEST.json)
    for p in release_dir.rglob("*"):
        if p.is_file():
            rel_p = p.relative_to(release_dir).as_posix()
            if rel_p == "MANIFEST.json":
                continue
            if rel_p not in expected_files:
                errors.append(f"Unexpected file present in release tree: {rel_p}")

    return {
        "ok": len(errors) == 0,
        "component": manifest.get("component"),
        "version": manifest.get("version"),
        "release_id": manifest.get("release_id"),
        "created_at": manifest.get("created_at"),
        "source_commit": manifest.get("source_commit"),
        "total_files": len(expected_files),
        "errors": errors,
    }


def list_releases(
    releases_base_dir: Path | None = None, component: str = "unified-agent"
) -> list[dict[str, Any]]:
    """List all releases for a component with status and active marker."""
    _validate_name_component(component, "component")

    if releases_base_dir is None:
        releases_base_dir = config.data_home() / "releases"

    comp_dir = releases_base_dir / component
    if not comp_dir.is_dir():
        return []

    current_symlink = comp_dir / "current"
    current_target = current_symlink.resolve().name if current_symlink.is_symlink() else None

    results: list[dict[str, Any]] = []
    for entry in sorted(comp_dir.iterdir(), reverse=True):
        if entry.is_dir() and not entry.is_symlink():
            verify_res = verify_release(entry)
            results.append(
                {
                    "release_id": entry.name,
                    "path": str(entry),
                    "is_current": (entry.name == current_target),
                    "is_valid": verify_res.get("ok", False),
                    "version": verify_res.get("version"),
                    "created_at": verify_res.get("created_at"),
                    "errors": verify_res.get("errors", []),
                }
            )

    return results


def rollback_release(
    releases_base_dir: Path | None = None,
    component: str = "unified-agent",
    target_release_id: str | None = None,
    authorize: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Roll back active component release to a target release or prior valid release."""
    _validate_name_component(component, "component")
    if target_release_id:
        _validate_name_component(target_release_id, "target_release_id")

    if releases_base_dir is None:
        releases_base_dir = config.data_home() / "releases"

    comp_dir = releases_base_dir / component
    current_symlink = comp_dir / "current"

    current_release_id = current_symlink.resolve().name if current_symlink.is_symlink() else None

    all_releases = list_releases(releases_base_dir, component)
    valid_releases = [r for r in all_releases if r["is_valid"]]

    if not valid_releases:
        return {"ok": False, "error": f"No valid releases found for {component}"}

    if target_release_id:
        target_rel = next((r for r in valid_releases if r["release_id"] == target_release_id), None)
        if not target_rel:
            return {
                "ok": False,
                "error": f"Target release '{target_release_id}' is not a valid release.",
            }
    else:
        if current_release_id:
            other_releases = [r for r in valid_releases if r["release_id"] != current_release_id]
            if not other_releases:
                return {
                    "ok": False,
                    "error": "No prior valid release available for rollback.",
                }
            target_rel = other_releases[0]
        else:
            target_rel = valid_releases[0]

    target_id = target_rel["release_id"]
    target_dir = comp_dir / target_id

    verif = verify_release(target_dir)
    if not verif["ok"]:
        return {
            "ok": False,
            "error": f"Target release verification failed: {verif['errors']}",
        }

    proof = {
        "ok": True,
        "component": component,
        "previous_release": current_release_id,
        "target_release": target_id,
        "target_path": str(target_dir),
        "checksum_verified": True,
        "timestamp": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "authorized": authorize,
        "dry_run": dry_run,
        "switched": False,
    }

    if not authorize:
        proof["warning"] = (
            "Rollback rehearsal successful. Authorization flag '--authorize' required to perform live symlink switch."
        )
        return proof

    if dry_run:
        proof["warning"] = "Dry run mode active. No symlink was modified."
        return proof

    tmp_symlink = comp_dir / f"current_tmp_rollback_{proof['timestamp']}"
    if tmp_symlink.is_symlink() or tmp_symlink.exists():
        tmp_symlink.unlink()
    tmp_symlink.symlink_to(target_id)
    tmp_symlink.replace(current_symlink)
    proof["switched"] = True

    return proof
