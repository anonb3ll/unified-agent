"""Installable catalog adapter over reviewed skills and existing org revisions.

No catalog store: skill_versions/assignments and the org revision evidence chain
remain authoritative. New versions attest every package member; legacy versions
without a full attestation keep the conservative supporting-file refusal.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_access,
    config,
    config_revisions,
    events,
    gateway,
    org_registry,
    profiles,
    safety,
    skills_registry,
    storage,
    trust_presets,
)
from .tools import approvals, files

_INSTALL_LOCK = threading.RLock()
_HASH = re.compile(r"[0-9a-f]{64}")
_LOCAL_ASSET = re.compile(r"(?:\b(?:scripts|references|assets|resources)/|\]\(([^)\s]+))")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError(f"{label} must be a positive integer")
    return value


def _assert_hash(value: Any, label: str = "content_hash") -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ToolError(f"{label} must be a SHA-256 hash")
    return value


def require_tool(tool: str) -> None:
    """HTTP and direct adapters cannot rely on MCP's ambient-session shortcut."""
    if not config.verified_agent_identity():
        raise ToolError("authenticated identity required")
    if not profiles.tool_is_allowed_for(profiles.current(), tool):
        raise ToolError(f"tool {tool!r} is not exposed by the active {profiles.current()} profile")


@contextmanager
def _governed(tool: str, payload: dict, *, read_only: bool = False, join_action: bool = False):
    require_tool(tool)
    agent_access.enforce(tool, read_only=read_only, production=not read_only)
    trust_presets.check_call(tool, read_only=read_only, payload=payload)
    with gateway.invocation(tool), approvals.approval_invocation(join_existing=join_action):
        safety.check_policy_gate(tool, payload, originator=config.verified_agent_identity())
        yield


def permissions() -> dict[str, bool]:
    profile = profiles.current()
    registry = profiles.tool_is_allowed_for(profile, "skill_registry")
    return {
        "drift": registry,
        "publish": registry,
        "install": registry and profiles.tool_is_allowed_for(profile, "fs_write"),
        "team_install": registry
        and profiles.tool_is_allowed_for(profile, "config_revision_manage"),
    }


def _support_refusal(row: dict) -> str | None:
    """Fail closed where the registry cannot attest an entire skill package."""
    if row.get("package_manifest") is not None:
        # get() verifies all stored hashes and bytes before this adapter sees it.
        return None
    content = str(row.get("content") or "")
    for match in _LOCAL_ASSET.finditer(content):
        target = match.group(1)
        if target is None or (
            not target.startswith("#")
            and urlsplit(target).scheme not in {"https", "http", "mailto"}
        ):
            return "Skill requires supporting files; this registry reviews SKILL.md only."
    try:
        source = safety.resolve_for_audit(str(row["source"]))
        directory = source if source.is_dir() else source.parent
        if not directory.is_dir():
            return "Published source package is unavailable; supporting files cannot be verified."
        for child in directory.iterdir():
            if child.name != "SKILL.md":
                return "Skill source contains supporting files that are not covered by its reviewed hash."
        manifest = directory / "SKILL.md"
        if manifest.is_symlink() or not manifest.is_file():
            return "Published source package is unsafe or unavailable."
    except (OSError, ToolError, ValueError):
        return "Published source package is unavailable or outside the permitted read scope."
    return None


def _catalog_read_tool() -> str:
    # The human Control Center operator already has crew_view status authority.
    # Keep the mixed write manager hidden; this adapter only projects metadata.
    if profiles.current() == profiles.OPERATOR and profiles.tool_is_allowed_for(
        profiles.current(), "crew_view"
    ):
        return "crew_view"
    return "skill_registry"


def catalog(surface_dir: str | None = None) -> dict:
    if surface_dir is not None and not isinstance(surface_dir, str):
        raise ToolError("surface_dir must be a string")
    if surface_dir and _catalog_read_tool() != "skill_registry":
        raise ToolError("surface drift requires skill_registry profile authority")
    with _governed(
        _catalog_read_tool(),
        {"action": "catalog", "params": {"surface_dir": surface_dir}},
        read_only=True,
    ):
        listed = skills_registry.list_skills()["skills"]
        latest: dict[str, int] = {}
        for row in listed:
            latest[row["name"]] = max(latest.get(row["name"], 0), row["version"])
        entries = []
        for row in listed:
            detail = skills_registry.get(row["name"], row["version"])
            refusal = _support_refusal(detail)
            entries.append(
                {
                    **row,
                    "latest": row["version"] == latest[row["name"]],
                    "installable": refusal is None,
                    "install_block_reason": refusal,
                    "provenance": {
                        "source": row["source"],
                        "actor": row["published_by"],
                        "ts": row["published_at"],
                    },
                }
            )
        with config_revisions._lock:
            document, _raw = config_revisions._live_org()
            offices = org_registry.load_registry()
            history = config_revisions.list_revisions("org")
            snapshots = config_revisions.revision_snapshots("org")
        captured_definitions: dict[tuple[str, str], dict] = {}
        for snapshot in snapshots:
            for definition in snapshot["document"]["offices"]:
                captured_definitions.setdefault((definition["id"], _hash(definition)), snapshot)
        definitions = {row["id"]: row for row in document["offices"]}
        teams = []
        shown: set[tuple[str, str]] = set()
        for office in offices:
            definition = definitions[office["id"]]
            team = _team_entry(
                definition,
                captured_definitions.get((definition["id"], _hash(definition))),
                drift=history["drift"],
                latest=True,
            )
            teams.append(team)
            shown.add((team["id"], team["content_hash"]))
        for snapshot in snapshots:
            for definition in snapshot["document"]["offices"]:
                key = (definition["id"], _hash(definition))
                if key not in shown:
                    teams.append(_team_entry(definition, snapshot, drift=False, latest=False))
                    shown.add(key)
        surfaces = []
        if surface_dir:
            try:
                root = _surface_read_root(surface_dir)

                def guard(path: Path) -> Path:
                    resolved = _surface_read_root(str(path))
                    if not resolved.is_relative_to(root):
                        raise ToolError("surface drift refuses paths outside its scoped root")
                    return resolved

                result = skills_registry._drift_resolved(root, path_guard=guard)
            except (ToolError, OSError) as exc:
                # A new destination can be installed before it exists. A drift
                # failure describes this surface, not the independent catalog.
                result = {"error": str(exc)[:300]}
            surfaces.append({"surface_dir": surface_dir, "result": result})
        from . import team_templates

        return {
            "starter_templates": team_templates.catalog(),
            "skills": entries,
            "teams": teams,
            "surfaces": surfaces,
            "permissions": permissions(),
            "as_of": storage.now(),
        }


def _declared_versions(raw: Any) -> tuple[list[dict], str | None]:
    normalized = []
    invalid = not isinstance(raw, list)
    for row in raw if isinstance(raw, list) else []:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not re.fullmatch(skills_registry.KEBAB_RE, row["name"])
            or len(row["name"]) > skills_registry.NAME_MAX
            or isinstance(row.get("version"), bool)
            or not isinstance(row.get("version"), int)
            or row["version"] <= 0
            or not isinstance(row.get("content_hash"), str)
            or not _HASH.fullmatch(row["content_hash"])
        ):
            invalid = True
            continue
        normalized.append(
            {"name": row["name"], "version": row["version"], "content_hash": row["content_hash"]}
        )
    return (
        normalized,
        "Invalid declared skill version metadata; excluded from the catalog projection."
        if invalid
        else None,
    )


def _team_entry(definition: dict, revision: dict | None, *, drift: bool, latest: bool) -> dict:
    declared, declared_error = _declared_versions(definition.get("skill_versions", []))
    members = []
    for label in dict.fromkeys(definition["members"]):
        profile = profiles.default_for_identity(label)
        resolved = skills_registry.resolve(label, profile=profile)
        members.append(
            {
                "label": label,
                "profile": profile,
                "role": "lead" if label == definition["lead"] else "member",
                "skills": [
                    {
                        "name": skill["name"],
                        "version": skill["version"],
                        "content_hash": skill["hash"],
                        "source": skill["source"],
                    }
                    for skill in resolved["skills"]
                ],
            }
        )
    return {
        "id": definition["id"],
        "name": str(definition.get("name") or definition["id"]),
        "lead": definition["lead"],
        "members": members,
        "responsibility": list(definition.get("decision_rights", [])),
        "permitted_scope": {
            "owned_epics": list(definition.get("owned_epics", [])),
            "default_lanes": list(definition.get("default_lanes", [])),
            "decision_rights": list(definition.get("decision_rights", [])),
            "grants_authority": False,
        },
        "declared_skill_versions": declared,
        "declared_skill_versions_error": declared_error,
        "skill_binding_source": "skill_assignments (current)",
        "version": revision["revision"] if revision else None,
        "content_hash": _hash(definition),
        "provenance": {
            "source": str(org_registry.registry_path()),
            "revision": revision["revision"] if revision else None,
            "actor": revision.get("actor") if revision else None,
            "ts": revision.get("ts") if revision else None,
        },
        "drift": drift,
        "latest": latest,
    }


def _surface_read_root(surface_dir: str) -> Path:
    """Resolve exactly the caller's surface without widening global audit roots."""
    candidate = Path(surface_dir).expanduser()
    if not candidate.is_absolute():
        root = safety.get_active_worktree_path() or config.workspace_root().resolve()
        candidate = root / candidate
    try:
        return safety.resolve_in_workspace(str(candidate))
    except ToolError:
        # Preserve explicitly auditable surfaces while keeping the relative
        # target bound to the caller's workspace/worktree, never another root.
        return safety.resolve_for_audit(str(candidate))


def _destination(surface_dir: str, name: str) -> Path:
    if not isinstance(surface_dir, str) or not surface_dir.strip():
        raise ToolError("surface_dir must be a non-empty workspace path")
    candidate = Path(surface_dir).expanduser() / name / "SKILL.md"
    root = safety.get_active_worktree_path() or config.workspace_root().resolve()
    unresolved = candidate if candidate.is_absolute() else root / candidate
    for ancestor in (unresolved, *unresolved.parents):
        if ancestor.is_symlink():
            raise ToolError("skill installation refuses symlink destinations")
    # Preserve the canonical writer's relative path semantics. An explicit
    # absolute destination still receives the same workspace/worktree boundary.
    destination = safety.resolve_in_workspace(str(candidate), protect_configured_hooks=True)
    if unresolved.resolve() != destination:
        raise ToolError("active worktree changed during destination resolution")
    return destination


def publish(name: str, path: str) -> dict:
    """Catalog publication rejects assets the immutable registry cannot attest."""
    require_tool("skill_registry")
    name = skills_registry._validate_name(name)
    return skills_registry.publish(name, path)


def install(
    name: str, version: int, content_hash: str, surface_dir: str, expected_hash: str | None = None
) -> dict:
    require_tool("skill_registry")
    require_tool("fs_write")
    version = _positive_int(version, "version")
    content_hash = _assert_hash(content_hash)
    row = skills_registry.get(name, version)
    if row is None:
        raise ToolError(f"unknown skill version: {name!r} v{version}")
    actual_hash = hashlib.sha256(row["content"].encode()).hexdigest()
    if row["content_hash"] != content_hash or (
        row.get("package_manifest") is None and actual_hash != content_hash
    ):
        raise ToolError("skill content hash does not match the reviewed registry version")
    refusal = _support_refusal(row)
    if refusal:
        raise ToolError(refusal)
    if row.get("package_manifest") is not None:
        return _install_package(row, surface_dir, expected_hash)
    destination = _destination(surface_dir, row["name"])
    payload = {
        "path": str(destination),
        "content": row["content"],
        "append": False,
        "expected_content_hash": expected_hash or "",
    }
    gate_payload = {
        "path": str(destination),
        "append": False,
        "content_sha256": content_hash,
        "bytes": len(row["content"].encode()),
        "expected_content_sha256": expected_hash or "",
    }
    if expected_hash is not None:
        _assert_hash(expected_hash, "expected_hash")
    with _INSTALL_LOCK, _governed("fs_write", payload, join_action=True):
        files._refuse_imessage_bridge_write(destination)
        if (
            destination.is_file()
            and destination.stat().st_size > skills_registry.MAX_SKILL_FILE_BYTES
        ):
            raise ToolError("existing skill file exceeds the comparison limit")
        before = (
            hashlib.sha256(destination.read_bytes()).hexdigest() if destination.exists() else None
        )
        if before == content_hash:
            return {
                "installed": False,
                "unchanged": True,
                "name": name,
                "version": version,
                "content_hash": content_hash,
                "path": str(destination),
                "provenance": {
                    "source": row["source"],
                    "actor": row["published_by"],
                    "ts": row["published_at"],
                },
            }
        if before != expected_hash:
            raise ToolError(
                "existing skill changed or contains local edits; supply its exact expected_hash to replace"
            )
        approvals.check_approval_gate(
            "fs_write", gate_payload, requested_by=config.verified_agent_identity()
        )
        # Approval may wait or be serviced by an adapter hook. Revalidate the
        # destination and compare after the gate before using the existing writer.
        if _destination(surface_dir, row["name"]) != destination:
            raise ToolError("skill destination changed during approval")
        current = (
            hashlib.sha256(destination.read_bytes()).hexdigest() if destination.exists() else None
        )
        if current != before:
            raise ToolError("existing skill changed during approval; retry with its current hash")
        refusal = _support_refusal(row)
        if refusal:
            raise ToolError(refusal)
        files.fs_write(str(destination), row["content"], expected_content_hash=expected_hash or "")
        result = {
            "installed": True,
            "name": name,
            "version": version,
            "content_hash": content_hash,
            "path": str(destination),
            "provenance": {
                "source": row["source"],
                "actor": row["published_by"],
                "ts": row["published_at"],
            },
        }
        result["evidence_ref"] = events.record_event(
            "agent.skill.installed", result, originator=config.verified_agent_identity()
        )
        return result


def _install_package(row: dict, surface_dir: str, expected_hash: str | None) -> dict:
    if expected_hash is not None:
        _assert_hash(expected_hash, "expected_hash")
    destination = _destination(surface_dir, row["name"])
    root = destination.parent
    members = skills_registry.version_files(row["name"], row["version"])
    payload = {
        "path": str(destination),
        "append": False,
        "content_sha256": row["content_hash"],
        "package_manifest": row["package_manifest"],
        "expected_content_sha256": expected_hash or "",
    }
    with _INSTALL_LOCK, _governed("fs_write", payload, join_action=True):
        if root.exists():
            existing = skills_registry._capture_package(root, "installation comparison")
            before = skills_registry._package_hash(skills_registry._package_manifest(existing))
        else:
            before = None
        result = {
            "name": row["name"],
            "version": row["version"],
            "content_hash": row["content_hash"],
            "package_manifest": row["package_manifest"],
            "path": str(destination),
            "provenance": {
                "source": row["source"],
                "actor": row["published_by"],
                "ts": row["published_at"],
            },
        }
        if before == row["content_hash"]:
            return {**result, "installed": False, "unchanged": True}
        if before != expected_hash:
            raise ToolError(
                "existing skill changed or contains local edits; supply its exact expected_hash to replace"
            )
        files._write_skill_package(
            str(root), members, expected_hash=expected_hash or "", content_hash=row["content_hash"]
        )
        result["installed"] = True
        result["evidence_ref"] = events.record_event(
            "agent.skill.installed", result, originator=config.verified_agent_identity()
        )
        return result


def install_team(team_id: str, revision: int, content_hash: str) -> dict:
    require_tool("skill_registry")
    require_tool("config_revision_manage")
    revision = _positive_int(revision, "revision")
    content_hash = _assert_hash(content_hash)
    if not isinstance(team_id, str) or not re.fullmatch(org_registry.OFFICE_ID_PATTERN, team_id):
        raise ToolError("invalid team_id")
    payload = {
        "action": "commit",
        "kind": "org",
        "revision": revision,
        "team_id": team_id,
        "content_hash": content_hash,
    }
    with _governed("config_revision_manage", payload, join_action=True), config_revisions._lock:
        snapshot = config_revisions.show_revision("org", revision)
        definition = next(
            (row for row in snapshot["document"]["offices"] if row["id"] == team_id), None
        )
        if definition is None:
            raise ToolError("team does not exist in the requested org revision")
        if _hash(definition) != content_hash:
            raise ToolError("team definition hash does not match the captured org revision")
        live, raw = config_revisions._live_org()
        planned = {
            **live,
            "offices": [definition if row["id"] == team_id else row for row in live["offices"]],
        }
        if not any(row["id"] == team_id for row in live["offices"]):
            planned["offices"].append(definition)
        # Existing config validation and one-shot approval seam; bind the pass
        # to both the installed definition and the whole live file we preserve.
        config_revisions._validate_org_text(config_revisions._dump(planned))
        if _hash(live) == _hash(planned):
            return {
                "installed": False,
                "unchanged": True,
                "team_id": team_id,
                "revision": revision,
                "content_hash": content_hash,
            }
        gate = {
            "kind": "org",
            "team_id": team_id,
            "revision": revision,
            "document_sha256": _hash(planned),
            "team_sha256": content_hash,
            "base_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
        approvals.check_approval_gate(
            config_revisions.COMMIT_TOOL,
            gate,
            requested_by=config.verified_agent_identity(),
            force=True,
        )
        if config_revisions._live_org()[1] != raw:
            raise ToolError("live org changed during approval; request installation again")
        try:
            result = config_revisions.commit("org", planned)
        except config_revisions.ConfigFanoutError as exc:
            exc.result.update(
                {
                    "installed": True,
                    "team_id": team_id,
                    "content_hash": content_hash,
                    "source_revision": revision,
                    "provenance": {
                        key: snapshot.get(key)
                        for key in ("revision", "actor", "ts", "ulid", "document_sha256")
                    },
                }
            )
            raise
        return {
            "installed": True,
            "team_id": team_id,
            "content_hash": content_hash,
            "source_revision": revision,
            "provenance": {
                key: snapshot.get(key)
                for key in ("revision", "actor", "ts", "ulid", "document_sha256")
            },
            **result,
        }


def call(action: str, params: dict) -> dict:
    """Shared HTTP call uses the registered MCP policy/approval wrapper."""
    if action == "catalog" and _catalog_read_tool() == "crew_view":
        return catalog(surface_dir=params.get("surface_dir"))
    require_tool("skill_registry")
    if action not in {
        "catalog",
        "publish",
        "install",
        "team_install",
        "starter_preview",
        "starter_install",
    }:
        raise ToolError(
            "unknown catalog action; choose catalog, publish, install, team_install, starter_preview or starter_install"
        )
    from . import tool_surface

    if action == "publish":
        params = {**params, "catalog_only": True}
    return tool_surface.guard(skills_registry.skill_registry)(action=action, params=params)
