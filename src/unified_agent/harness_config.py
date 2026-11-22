"""Hub-served harness config bundles (task #1330, I4).

One versioned bundle on the hub is the source of truth for skills, rules text,
and MCP server lists. ``ua-node`` renders per-harness files, reports drift, and
never writes red-tier surfaces until a human approves the exact proposal.

MCP entries must use OAuth (or ``none``) — bearer tokens in the bundle are
refused at publish time so devices never re-grow hand-copied secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from difflib import unified_diff
from pathlib import Path
from typing import Any

from . import edition, events, node_registry, storage

APPLY_TOOL = "harness_config_apply"
PUBLISH_TOOL = "harness_config_publish"
CHANNEL = "agent.harness_config"

MAX_BUNDLE_BYTES = 512_000
MAX_FILE_BYTES = 256_000
MAX_DIFF_CHARS = 80_000
MAX_PROPOSALS_PER_REPORT = 32
MAX_SKILLS = 64
MAX_MCP_SERVERS = 32

_SURFACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+-]{0,255}$")

# Default relative paths under UA_HARNESS_CONFIG_ROOT (proposal/apply sandbox).
DEFAULT_SURFACES: dict[str, dict[str, str]] = {
    "claude": {"path": "CLAUDE.md", "kind": "rules"},
    "agents": {"path": "AGENTS.md", "kind": "rules"},
    "cursor": {"path": ".cursorrules", "kind": "rules"},
    "codex": {"path": "codex/config.toml", "kind": "mcp"},
    "opencode": {"path": "opencode/opencode.json", "kind": "mcp"},
}

# Rules surfaces carry text only; every other surface is named for the harness
# it configures and renders the bundle's skills.
_RULES_SURFACES = ("claude", "agents", "cursor")


def skill_surfaces(surfaces: dict[str, dict[str, str]]) -> set[str]:
    """Surface names whose rendered config delivers the bundle's skills."""
    return {
        name
        for name, meta in surfaces.items()
        if (meta.get("kind") or "rules") != "rules" and name not in _RULES_SURFACES
    }


_OK_MCP_AUTH = frozenset({"oauth", "oauth2", "none"})
_FORBIDDEN_MCP_AUTH = frozenset({"bearer", "token", "apikey", "api_key", "header"})


class HarnessConfigError(ValueError):
    """Invalid bundle or device report."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso() -> str:
    return storage.now()


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_root() -> Path:
    raw = os.environ.get("UA_HARNESS_CONFIG_ROOT") or ""
    if raw.strip():
        return Path(raw).expanduser()
    home = Path(os.environ.get("UA_HOME") or "~/.unified_agent").expanduser()
    return home / "harness-config"


def _bounded_str(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarnessConfigError("expected a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise HarnessConfigError(f"string exceeds {limit} characters")
    return text


def _validate_mcp_servers(servers: Any) -> list[dict[str, Any]]:
    if servers is None:
        return []
    if not isinstance(servers, list):
        raise HarnessConfigError("mcp_servers must be a list")
    if len(servers) > MAX_MCP_SERVERS:
        raise HarnessConfigError(f"at most {MAX_MCP_SERVERS} mcp_servers")
    out: list[dict[str, Any]] = []
    for raw in servers:
        if not isinstance(raw, dict):
            raise HarnessConfigError("each mcp_server must be an object")
        name = _bounded_str(raw.get("name"), 64)
        if not name:
            raise HarnessConfigError("mcp_server.name is required")
        url = _bounded_str(raw.get("url") or raw.get("serverUrl") or raw.get("server_url"), 500)
        if not url:
            raise HarnessConfigError(f"mcp_server {name!r} needs a url")
        if not url.startswith(("https://", "http://")):
            raise HarnessConfigError(f"mcp_server {name!r} url must be http(s)")
        auth_raw = raw.get("auth") or raw.get("auth_type") or "oauth"
        auth = str(auth_raw).strip().lower()
        if auth in _FORBIDDEN_MCP_AUTH or "bearer" in auth:
            raise HarnessConfigError(
                f"mcp_server {name!r} must use OAuth (or none), not bearer/token auth"
            )
        if auth not in _OK_MCP_AUTH:
            raise HarnessConfigError(
                f"mcp_server {name!r} auth must be one of {sorted(_OK_MCP_AUTH)}"
            )
        # Refuse embedded secrets even when auth says oauth.
        for key in ("token", "bearer", "authorization", "api_key", "apiKey", "headers"):
            if raw.get(key):
                raise HarnessConfigError(
                    f"mcp_server {name!r} must not embed {key!r}; use OAuth on the hub"
                )
        entry = {"name": name, "url": url, "auth": auth}
        if raw.get("transport"):
            entry["transport"] = _bounded_str(raw.get("transport"), 32) or "http"
        out.append(entry)
    return out


def _validate_skills(skills: Any) -> list[dict[str, Any]]:
    if skills is None:
        return []
    if not isinstance(skills, list):
        raise HarnessConfigError("skills must be a list")
    if len(skills) > MAX_SKILLS:
        raise HarnessConfigError(f"at most {MAX_SKILLS} skills")
    out: list[dict[str, Any]] = []
    for raw in skills:
        if not isinstance(raw, dict):
            raise HarnessConfigError("each skill must be an object")
        name = _bounded_str(raw.get("name"), 64)
        if not name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise HarnessConfigError("skill.name must be kebab-case ≤64 chars")
        description = _bounded_str(raw.get("description"), 1024) or ""
        body = raw.get("body") or raw.get("content") or ""
        if not isinstance(body, str):
            raise HarnessConfigError(f"skill {name!r} body must be a string")
        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            raise HarnessConfigError(f"skill {name!r} body exceeds size limit")
        out.append({"name": name, "description": description, "body": body})
    return out


def _validate_rules(rules: Any) -> dict[str, str]:
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise HarnessConfigError("rules must be an object")
    out: dict[str, str] = {}
    for key, value in rules.items():
        surface = str(key).strip().lower()
        if not _SURFACE_RE.fullmatch(surface):
            raise HarnessConfigError(f"invalid rules surface {key!r}")
        if not isinstance(value, str):
            raise HarnessConfigError(f"rules.{surface} must be a string")
        if len(value.encode("utf-8")) > MAX_FILE_BYTES:
            raise HarnessConfigError(f"rules.{surface} exceeds size limit")
        out[surface] = value
    return out


def _validate_surfaces(surfaces: Any) -> dict[str, dict[str, str]]:
    if surfaces is None:
        return dict(DEFAULT_SURFACES)
    if not isinstance(surfaces, dict):
        raise HarnessConfigError("surfaces must be an object")
    out: dict[str, dict[str, str]] = {}
    for key, raw in surfaces.items():
        surface = str(key).strip().lower()
        if not _SURFACE_RE.fullmatch(surface):
            raise HarnessConfigError(f"invalid surface {key!r}")
        if not isinstance(raw, dict):
            raise HarnessConfigError(f"surfaces.{surface} must be an object")
        path = _bounded_str(raw.get("path"), 256)
        if not path or not _PATH_RE.fullmatch(path) or ".." in path.split("/"):
            raise HarnessConfigError(f"surfaces.{surface}.path is invalid")
        kind = _bounded_str(raw.get("kind"), 32) or "rules"
        out[surface] = {"path": path, "kind": kind}
    return out or dict(DEFAULT_SURFACES)


def normalize_bundle(raw: Any) -> dict[str, Any]:
    """Validate and canonicalize a publish payload."""
    if not isinstance(raw, dict):
        raise HarnessConfigError("bundle must be a JSON object")
    version = _bounded_str(raw.get("version"), 64)
    if not version or not _VERSION_RE.fullmatch(version):
        raise HarnessConfigError("version is required (alphanumeric, ._- , ≤64)")
    bundle = {
        "version": version,
        "rules": _validate_rules(raw.get("rules")),
        "skills": _validate_skills(raw.get("skills")),
        "mcp_servers": _validate_mcp_servers(raw.get("mcp_servers")),
        "surfaces": _validate_surfaces(raw.get("surfaces")),
    }
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise HarnessConfigError("bundle exceeds size limit")
    return bundle


def render_surface(bundle: dict[str, Any], surface: str) -> str:
    """Produce the desired on-disk text for one harness surface."""
    surface = surface.strip().lower()
    surfaces = bundle.get("surfaces") or DEFAULT_SURFACES
    meta = surfaces.get(surface) or DEFAULT_SURFACES.get(surface)
    if meta is None:
        raise HarnessConfigError(f"unknown surface {surface!r}")
    kind = meta.get("kind") or "rules"
    rules = bundle.get("rules") or {}
    skills = bundle.get("skills") or []
    mcp = bundle.get("mcp_servers") or []

    if kind == "rules" or surface in ("claude", "agents", "cursor"):
        text = rules.get(surface)
        if text is None and surface == "agents":
            text = rules.get("claude")
        if text is None:
            text = (
                f"# Hub-managed {surface} rules\n"
                f"# bundle {bundle.get('version')}\n"
                "# (empty — publish rules.<surface> to fill)\n"
            )
        return text if text.endswith("\n") else text + "\n"

    if surface == "codex" or kind == "mcp" and surface == "codex":
        lines = [
            f"# Generated by unified-agent harness_config bundle {bundle['version']}",
            "# Do not hand-edit; approve a new proposal from Control Center.",
            "",
            "[mcp]",
        ]
        for server in mcp:
            lines.append(f"[mcp.servers.{server['name']}]")
            lines.append(f'url = "{server["url"]}"')
            lines.append(f'auth = "{server["auth"]}"')
            lines.append("")
        if skills:
            lines.append("[skills]")
            for skill in skills:
                lines.append(f"[skills.{skill['name']}]")
                desc = (skill.get("description") or "").replace('"', '\\"')
                lines.append(f'description = "{desc}"')
                lines.append("")
        rules_text = rules.get("codex") or rules.get("agents")
        if rules_text:
            lines.append("[ua.rules]")
            lines.append('text = """')
            lines.append(rules_text.rstrip("\n"))
            lines.append('"""')
            lines.append("")
        return "\n".join(lines)

    # opencode / generic mcp+json
    payload = {
        "ua_bundle_version": bundle["version"],
        "mcp": {
            s["name"]: {"url": s["url"], "auth": s["auth"], "type": s.get("transport") or "remote"}
            for s in mcp
        },
        "skills": [
            {
                "name": s["name"],
                "description": s.get("description") or "",
                "body": s.get("body") or "",
            }
            for s in skills
        ],
    }
    rules_text = rules.get(surface) or rules.get("agents")
    if rules_text:
        payload["rules"] = rules_text
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_all(bundle: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map surface → {path, content, hash} for every declared surface."""
    surfaces = bundle.get("surfaces") or DEFAULT_SURFACES
    out: dict[str, dict[str, str]] = {}
    for surface, meta in surfaces.items():
        content = render_surface(bundle, surface)
        out[surface] = {
            "path": meta["path"],
            "kind": meta.get("kind") or "rules",
            "content": content,
            "hash": content_hash(content),
        }
    return out


def make_diff(path: str, observed: str | None, desired: str) -> str:
    left = (observed or "").splitlines(keepends=True)
    right = desired.splitlines(keepends=True)
    if not left and not observed:
        left = []
    diff = "".join(
        unified_diff(
            left,
            right,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        return diff[: MAX_DIFF_CHARS - 40] + "\n# … diff truncated …\n"
    return diff


def resolve_surface_path(relative: str, *, root: Path | None = None) -> Path:
    """Resolve a surface path under the harness-config root; refuse escapes."""
    base = (root or config_root()).resolve()
    rel = relative.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise HarnessConfigError("path must not contain ..")
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HarnessConfigError("path escapes harness config root") from exc
    return target


def read_observed(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        raise HarnessConfigError(f"observed file {path} exceeds size limit")
    return data.decode("utf-8")


def compute_local_drift(
    bundle: dict[str, Any], *, root: Path | None = None
) -> list[dict[str, Any]]:
    """Compare rendered bundle to on-disk files under the config root."""
    rendered = render_all(bundle)
    proposals: list[dict[str, Any]] = []
    for surface, info in rendered.items():
        path = resolve_surface_path(info["path"], root=root)
        observed = read_observed(path)
        observed_hash = content_hash(observed) if observed is not None else None
        if observed_hash == info["hash"]:
            continue
        proposals.append(
            {
                "surface": surface,
                "path": info["path"],
                "desired_hash": info["hash"],
                "observed_hash": observed_hash,
                "diff": make_diff(info["path"], observed, info["content"]),
                "desired": info["content"],
            }
        )
    return proposals


def apply_surface(
    bundle: dict[str, Any], surface: str, *, root: Path | None = None
) -> dict[str, Any]:
    """Write one rendered surface under the config root (after human approval)."""
    rendered = render_all(bundle)
    info = rendered.get(surface)
    if info is None:
        raise HarnessConfigError(f"unknown surface {surface!r}", status=404)
    path = resolve_surface_path(info["path"], root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info["content"], encoding="utf-8")
    return {
        "surface": surface,
        "path": info["path"],
        "absolute": str(path),
        "hash": info["hash"],
        "bytes": len(info["content"].encode("utf-8")),
    }


# --- hub persistence ---------------------------------------------------------


def _record(kind: str, payload: dict[str, Any], originator: str) -> None:
    try:
        events.record_event(CHANNEL, {"kind": kind, **payload}, originator=originator)
    except Exception:
        pass


def active_bundle() -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = _row(
            conn.execute(
                "SELECT version, published_at, published_by, bundle_json FROM harness_config_bundles "
                "WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
    finally:
        conn.close()
    if row is None:
        return None
    try:
        bundle = json.loads(row["bundle_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(bundle, dict):
        return None
    return {
        "version": row["version"],
        "published_at": row["published_at"],
        "published_by": row["published_by"],
        "bundle": bundle,
    }


def publish_bundle(raw: Any, *, actor: str) -> dict[str, Any]:
    """Activate a new versioned bundle. Deactivates the previous active row."""
    bundle = normalize_bundle(raw)
    version = bundle["version"]
    now = _iso()
    conn = storage.connect()
    try:
        existing = conn.execute(
            "SELECT id FROM harness_config_bundles WHERE version=?", (version,)
        ).fetchone()
        if existing is not None:
            raise HarnessConfigError(f"version {version!r} already published", status=409)
        conn.execute("UPDATE harness_config_bundles SET active=0 WHERE active=1")
        cur = conn.execute(
            "INSERT INTO harness_config_bundles "
            "(version, published_at, published_by, bundle_json, active, org_id) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                version,
                now,
                actor,
                json.dumps(bundle, sort_keys=True, separators=(",", ":")),
                edition.tenant_org_id(),
            ),
        )
        conn.commit()
        bundle_id = int(cur.lastrowid)
    finally:
        conn.close()
    _record(
        "harness_config_publish",
        {"version": version, "bundle_id": bundle_id, "surfaces": list(bundle["surfaces"])},
        actor,
    )
    return {
        "ok": True,
        "version": version,
        "bundle_id": bundle_id,
        "published_at": now,
        "published_by": actor,
        "surfaces": sorted(bundle["surfaces"]),
        "mcp_servers": [s["name"] for s in bundle["mcp_servers"]],
        "skills": [s["name"] for s in bundle["skills"]],
    }


def _proposal_id() -> str:
    return f"hc-{secrets.token_hex(8)}"


def _write_inbox_proposal(
    *,
    node: str,
    surface: str,
    version: str,
    path: str,
    diff: str,
    proposal_id: str,
) -> str | None:
    """Best-effort vault Inbox note for human review. Never raises.

    Returns the vault-relative note path, or None if it could not be written.
    A ``locked: true`` note at the proposal's name (an operator annotating a
    pending proposal) is never overwritten (#2091): a re-report of the same
    node/surface/version files to the next free sibling (``...-v1-2.md``), and
    that path is what is returned, so the drift report's ``inbox`` field points
    at the note that holds this proposal.
    """
    try:
        from .note_lock import unlocked_note_slot, vault_rel
        from .safety import resolve_in_vault

        note = (
            f"# Harness config proposal `{proposal_id}`\n\n"
            f"- node: `{node}`\n"
            f"- surface: `{surface}`\n"
            f"- path: `{path}`\n"
            f"- bundle: `{version}`\n"
            f"- status: pending approval (`{APPLY_TOOL}`)\n\n"
            "Proposal-mode only — ua-node will not write this file until the operator "
            "approves the matching approval row.\n\n"
            "```diff\n"
            f"{diff[: MAX_DIFF_CHARS - 200]}\n"
            "```\n"
        )
        rel = f"00 - Inbox/harness-config/{node}-{surface}-{version}.md"
        target = resolve_in_vault(rel, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The per-note lock the hub's other note writers take (#2086).
        with unlocked_note_slot(target) as slot:
            slot.write_text(note, encoding="utf-8")
        return vault_rel(slot).replace("\\", "/")
    except Exception:
        return None


def report_drift(
    node: Any,
    proposals: Any,
    *,
    identity: str,
    device_node: str | None = None,
    bundle_version: str | None = None,
) -> dict[str, Any]:
    """Accept a ua-node drift report: park approvals + Inbox notes per surface."""
    name = node_registry.validate_node_name(node)
    conn = storage.connect()
    try:
        node_registry._owned_node(conn, name, identity, device_node)
    finally:
        conn.close()

    active = active_bundle()
    if active is None:
        raise HarnessConfigError("no active harness config bundle", status=404)
    version = active["version"]
    if bundle_version and bundle_version != version:
        raise HarnessConfigError(
            f"report targets {bundle_version!r} but active bundle is {version!r}",
            status=409,
        )
    if not isinstance(proposals, list):
        raise HarnessConfigError("proposals must be a list")
    if len(proposals) > MAX_PROPOSALS_PER_REPORT:
        raise HarnessConfigError(f"at most {MAX_PROPOSALS_PER_REPORT} proposals per report")

    from .tools import approvals as approvals_tool

    accepted: list[dict[str, Any]] = []
    for raw in proposals:
        if not isinstance(raw, dict):
            raise HarnessConfigError("each proposal must be an object")
        surface = _bounded_str(raw.get("surface"), 32)
        if not surface or not _SURFACE_RE.fullmatch(surface):
            raise HarnessConfigError("proposal.surface is invalid")
        path = _bounded_str(raw.get("path"), 256)
        if not path or ".." in path.split("/"):
            raise HarnessConfigError("proposal.path is invalid")
        desired_hash = _bounded_str(raw.get("desired_hash"), 64)
        if not desired_hash or len(desired_hash) != 64:
            raise HarnessConfigError("proposal.desired_hash must be sha256 hex")
        # Recompute desired from the active bundle so a node cannot invent content.
        try:
            desired = render_surface(active["bundle"], surface)
        except HarnessConfigError:
            raise
        if content_hash(desired) != desired_hash:
            raise HarnessConfigError(
                f"desired_hash for {surface!r} does not match active bundle render",
                status=409,
            )
        observed_hash = _bounded_str(raw.get("observed_hash"), 64)
        diff = raw.get("diff") if isinstance(raw.get("diff"), str) else ""
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[: MAX_DIFF_CHARS - 40] + "\n# … diff truncated …\n"
        if not diff:
            diff = make_diff(path, None, desired)

        canonical = {
            "node": name,
            "surface": surface,
            "path": path,
            "bundle_version": version,
            "desired_hash": desired_hash,
        }
        gate = approvals_tool.check_approval_gate(
            APPLY_TOOL, canonical, requested_by=identity, force=True, request_only=True
        )
        approval_id = gate.get("approval_id")
        status = gate.get("status") or "pending"

        now = _iso()
        conn = storage.connect()
        try:
            existing = _row(
                conn.execute(
                    "SELECT id, status FROM harness_config_proposals "
                    "WHERE node=? AND surface=? AND bundle_version=? AND desired_hash=? "
                    "ORDER BY id DESC LIMIT 1",
                    (name, surface, version, desired_hash),
                ).fetchone()
            )
            if existing and existing["status"] in ("pending", "approved"):
                proposal_id = existing["id"]
            else:
                proposal_id = _proposal_id()
                conn.execute(
                    "INSERT INTO harness_config_proposals "
                    "(id, node, surface, path, bundle_version, desired_hash, observed_hash, "
                    "diff_text, status, approval_id, created_at, updated_at, org_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        proposal_id,
                        name,
                        surface,
                        path,
                        version,
                        desired_hash,
                        observed_hash,
                        diff,
                        approval_id,
                        now,
                        now,
                        edition.tenant_org_id(),
                    ),
                )
            conn.execute(
                "INSERT INTO harness_config_drift "
                "(node, surface, path, bundle_version, observed_hash, desired_hash, reported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, surface, path, version, observed_hash, desired_hash, now),
            )
            conn.commit()
        finally:
            conn.close()

        inbox = None
        if status == "pending":
            inbox = _write_inbox_proposal(
                node=name,
                surface=surface,
                version=version,
                path=path,
                diff=diff,
                proposal_id=proposal_id,
            )
        accepted.append(
            {
                "proposal_id": proposal_id,
                "surface": surface,
                "path": path,
                "approval_id": approval_id,
                "status": status,
                "inbox": inbox,
            }
        )

    grants = list_apply_grants(name)
    return {
        "ok": True,
        "bundle_version": version,
        "proposals": accepted,
        "apply_grants": grants,
    }


def list_apply_grants(node: str) -> list[dict[str, Any]]:
    """Surfaces with an approved (unconsumed) apply pass for this node."""
    from .tools import approvals as approvals_tool

    name = node_registry.validate_node_name(node)
    active = active_bundle()
    if active is None:
        return []
    version = active["version"]
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, surface, path, desired_hash, approval_id, status "
            "FROM harness_config_proposals WHERE node=? AND bundle_version=? "
            "AND status IN ('pending', 'approved') ORDER BY created_at",
            (name, version),
        ).fetchall()
    finally:
        conn.close()

    grants: list[dict[str, Any]] = []
    for row in rows:
        proposal = _row(row)
        assert proposal is not None
        canonical = {
            "node": name,
            "surface": proposal["surface"],
            "path": proposal["path"],
            "bundle_version": version,
            "desired_hash": proposal["desired_hash"],
        }
        gate = approvals_tool.check_approval_gate(
            APPLY_TOOL,
            canonical,
            requested_by="ua-node",
            force=True,
            request_only=True,
        )
        if gate.get("status") == "approved":
            grants.append(
                {
                    "proposal_id": proposal["id"],
                    "surface": proposal["surface"],
                    "path": proposal["path"],
                    "desired_hash": proposal["desired_hash"],
                    "approval_id": gate.get("approval_id") or proposal.get("approval_id"),
                    "bundle_version": version,
                }
            )
    return grants


def mark_applied(
    node: Any,
    *,
    surface: str,
    desired_hash: str,
    identity: str,
    device_node: str | None = None,
) -> dict[str, Any]:
    """Consume the apply approval and mark the proposal applied after a node write."""
    name = node_registry.validate_node_name(node)
    surface_key = _bounded_str(surface, 32)
    if not surface_key or not _SURFACE_RE.fullmatch(surface_key):
        raise HarnessConfigError("surface is invalid")
    hash_key = _bounded_str(desired_hash, 64)
    if not hash_key or len(hash_key) != 64:
        raise HarnessConfigError("desired_hash must be sha256 hex")

    conn = storage.connect()
    try:
        node_registry._owned_node(conn, name, identity, device_node)
    finally:
        conn.close()

    active = active_bundle()
    if active is None:
        raise HarnessConfigError("no active harness config bundle", status=404)
    version = active["version"]
    desired = render_surface(active["bundle"], surface_key)
    if content_hash(desired) != hash_key:
        raise HarnessConfigError("desired_hash does not match active bundle", status=409)

    conn = storage.connect()
    try:
        proposal = _row(
            conn.execute(
                "SELECT * FROM harness_config_proposals "
                "WHERE node=? AND surface=? AND bundle_version=? AND desired_hash=? "
                "AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1",
                (name, surface_key, version, hash_key),
            ).fetchone()
        )
    finally:
        conn.close()
    if proposal is None:
        raise HarnessConfigError("no matching pending/approved proposal", status=404)

    canonical = {
        "node": name,
        "surface": surface_key,
        "path": proposal["path"],
        "bundle_version": version,
        "desired_hash": hash_key,
    }

    from .tools import approvals as approvals_tool

    gate = approvals_tool.check_approval_gate(
        APPLY_TOOL, canonical, requested_by=identity, force=True, request_only=False
    )
    now = _iso()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE harness_config_proposals SET status='applied', updated_at=?, "
            "resolved_at=?, resolved_by=? WHERE id=?",
            (now, now, identity, proposal["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    _record(
        "harness_config_applied",
        {
            "node": name,
            "surface": surface_key,
            "proposal_id": proposal["id"],
            "approval_id": gate.get("approval_id"),
            "bundle_version": version,
        },
        identity,
    )
    return {
        "ok": True,
        "proposal_id": proposal["id"],
        "surface": surface_key,
        "bundle_version": version,
        "approval_id": gate.get("approval_id"),
    }


def hub_status() -> dict[str, Any]:
    """Control Center / doctor snapshot."""
    active = active_bundle()
    conn = storage.connect()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM harness_config_proposals WHERE status='pending'"
        ).fetchone()["n"]
        recent = [
            _row(r)
            for r in conn.execute(
                "SELECT id, node, surface, path, bundle_version, status, created_at, approval_id "
                "FROM harness_config_proposals ORDER BY created_at DESC LIMIT 25"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "active": active,
        "pending_proposals": int(pending or 0),
        "recent_proposals": recent,
        "apply_tool": APPLY_TOOL,
        "proposal_mode": True,
    }


def node_config_payload(
    node: Any, *, identity: str, device_node: str | None = None
) -> dict[str, Any]:
    """GET /api/node/config body for a registered device."""
    name = node_registry.validate_node_name(node)
    conn = storage.connect()
    try:
        node_registry._owned_node(conn, name, identity, device_node)
    finally:
        conn.close()
    active = active_bundle()
    if active is None:
        return {
            "bundle": None,
            "version": None,
            "rendered": {},
            "apply_grants": [],
            "proposal_mode": True,
        }
    rendered = {
        surface: {"path": info["path"], "hash": info["hash"], "kind": info["kind"]}
        for surface, info in render_all(active["bundle"]).items()
    }
    # Include full content so the node can render without a second round-trip.
    full = render_all(active["bundle"])
    return {
        "version": active["version"],
        "published_at": active["published_at"],
        "published_by": active["published_by"],
        "bundle": active["bundle"],
        "rendered": {
            s: {
                "path": i["path"],
                "hash": i["hash"],
                "kind": i["kind"],
                "content": i["content"],
            }
            for s, i in full.items()
        },
        "surfaces": rendered,
        "apply_grants": list_apply_grants(name),
        "proposal_mode": True,
        "config_root_hint": str(config_root()),
    }


__all__ = [
    "APPLY_TOOL",
    "DEFAULT_SURFACES",
    "HarnessConfigError",
    "PUBLISH_TOOL",
    "active_bundle",
    "apply_surface",
    "compute_local_drift",
    "config_root",
    "content_hash",
    "hub_status",
    "list_apply_grants",
    "make_diff",
    "mark_applied",
    "node_config_payload",
    "normalize_bundle",
    "publish_bundle",
    "render_all",
    "read_observed",
    "render_surface",
    "report_drift",
    "resolve_surface_path",
]
