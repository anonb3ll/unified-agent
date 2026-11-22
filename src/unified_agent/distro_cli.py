"""``unified-agent-distro``: drop a portable unified-agent setup into a project.

``init <path>`` scaffolds the files any agent harness needs to work on a
project through the hub: a harness-neutral constitution (``AGENTS.md``) with
the hard rules, thin ``CLAUDE.md``/``GEMINI.md`` pointers to it, per-harness
MCP client config that reaches the hub over HTTP, an ``operating.yaml`` that
names the project's operating profile, and a starter skill (task #1733).

``check <path>`` reports missing or drifted files and any literal secret.

Rules this module keeps:

- Dry run by default. ``init`` prints the plan and writes only with
  ``--write``. A file whose content already matches is left alone, so a
  second run is a no-op.
- Never overwrites. A file that exists with different content is a conflict:
  ``--write`` refuses and lists every conflict unless ``--force`` is given.
  No ``*.bak`` copies are made (the repo ``.gitignore`` drops them).
- Config names environment variables and never holds a token. Claude Code
  gets a ``bin/ua-register-claude`` helper that registers the hub at user
  scope, because a project ``.mcp.json`` hub entry would shadow the
  authenticated user-scope registration.
- Writes stay inside the target: every destination is checked for symlinked
  components and for resolving outside the target directory.

Template sources live in ``templates/distro/`` under neutral names
(``constitution.md.tmpl`` rather than ``AGENTS.md``) and are mapped to their
target names here, so the package's own tree never contains files the review
pipeline treats as instruction or MCP config files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .audit.rules import (
    BEARER_RX,
    INSTRUCTION_FILES,
    MCP_CONFIG_PATHS,
    _literal_mcp_secret,
    _mask,
    check_hidden_unicode,
    check_risky_instructions,
    check_secrets,
)

HARNESSES = ("claude", "codex", "cursor", "gemini", "opencode", "copilot")
DEFAULT_PROFILE = "standard"

_PROFILE_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Rendered into JSON, TOML and a double-quoted shell string: no quotes,
# backslashes, whitespace, backticks or '$'. No '@', '?' or '#' either: a hub
# URL carries no userinfo, query or fragment, so it can never smuggle a
# credential into printed output or a generated file.
_HUB_URL_RX = re.compile(r"^https?://[A-Za-z0-9._~:/+,;=%\[\]-]+$")
_PLACEHOLDER_RX = re.compile(r"\{\{[a-z_]+\}\}")
_OPERATING_PROFILE_RX = re.compile(r"^operating_profile:\s*([^\s#]+)", re.MULTILINE)
# Hub bearer tokens carry a ``ua_`` prefix; require a digit so long
# snake_case identifiers do not match.
_HUB_TOKEN_RX = re.compile(r"\bua_(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{24,}")

# Extra harness config files ``check`` scans for secrets when present, beyond
# the files this tool manages.
_EXTRA_SCAN_PATHS = (*MCP_CONFIG_PATHS, "opencode.json", ".codex/config.toml")


def template_dir() -> Path:
    """The ``templates/distro/`` directory shipped inside the package."""
    return Path(__file__).resolve().parent / "templates" / "distro"


@dataclass(frozen=True)
class FileSpec:
    """One managed file: where it goes, which template renders it, who needs it."""

    dest: str
    template: str
    harness: str | None  # None = every harness selection gets it
    executable: bool = False
    # MCP config files that may already hold other servers: "toml" (Codex
    # [mcp_servers.<name>] tables) or "json:<servers key>". --force replaces
    # only the unified-agent entry in them and keeps every other server.
    merge: str | None = None


FILE_SPECS: tuple[FileSpec, ...] = (
    FileSpec("AGENTS.md", "constitution.md.tmpl", None),
    FileSpec(".unified-agent/operating.yaml", "operating.yaml.tmpl", None),
    FileSpec(".agents/skills/hub-session-start/SKILL.md", "skill_example.md.tmpl", None),
    FileSpec("CLAUDE.md", "claude_pointer.md.tmpl", "claude"),
    FileSpec(".claude/skills/hub-session-start/SKILL.md", "skill_example.md.tmpl", "claude"),
    FileSpec(".unified-agent/bin/ua-register-claude", "register_claude.sh.tmpl", "claude", True),
    FileSpec(".codex/config.toml", "codex_config.toml.tmpl", "codex", merge="toml"),
    FileSpec(".cursor/mcp.json", "cursor_mcp.json.tmpl", "cursor", merge="json:mcpServers"),
    FileSpec("GEMINI.md", "gemini_pointer.md.tmpl", "gemini"),
    FileSpec("opencode.json", "opencode.json.tmpl", "opencode", merge="json:mcp"),
    FileSpec(".unified-agent/bin/ua-opencode", "opencode_wrapper.sh.tmpl", "opencode", True),
    # Copilot in VS Code reads .vscode/mcp.json. The Copilot CLI does not, and
    # it does not document env expansion in MCP headers, so ua-copilot builds
    # its server config at launch in a private temp file outside the project.
    FileSpec(".vscode/mcp.json", "vscode_mcp.json.tmpl", "copilot", merge="json:servers"),
    FileSpec(".unified-agent/bin/ua-copilot", "copilot_wrapper.sh.tmpl", "copilot", True),
)

SERVER_NAME = "unified-agent"
# Harnesses whose generated config is a project-scoped hub entry. Under ~/dev
# such an entry would shadow the fleet's user-scope registration.
_PROJECT_HUB_HARNESSES = frozenset({"codex", "cursor", "opencode", "copilot"})


def _is_old_copilot_config(text: str) -> bool:
    """True only for the exact shape an earlier version generated (any hub URL).

    That file carried no token (only a ${UA_AGENT_BEARER_TOKEN} reference); it
    is obsolete because ua-copilot now builds the config at launch. Anything
    else at that path is the user's file and is never touched.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict) or set(data) != {"mcpServers"}:
        return False
    servers = data["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != {SERVER_NAME}:
        return False
    entry = servers[SERVER_NAME]
    return (
        isinstance(entry, dict)
        and set(entry) == {"type", "url", "headers", "tools"}
        and entry["type"] == "http"
        and isinstance(entry["url"], str)
        and entry["headers"] == {"Authorization": "Bearer ${UA_AGENT_BEARER_TOKEN}"}
        and entry["tools"] == ["*"]
    )


# Files earlier versions generated and this one no longer does:
# path -> (harness that generated it, recogniser). `init --write` removes a
# match and `check` reports it, but only when that harness is selected, so
# `init --harness codex` never touches a Copilot file.
OBSOLETE_FILES: dict[str, tuple[str, Callable[[str], bool]]] = {
    ".unified-agent/copilot-mcp.json": ("copilot", _is_old_copilot_config),
}


def _obsolete_recogniser(rel: str) -> Callable[[str], bool]:
    return OBSOLETE_FILES[rel][1]


def _obsolete_matches(target: Path, specs: list[FileSpec]) -> list[tuple[str, Path]]:
    """Obsolete generated files present in ``target`` for the selected harnesses."""
    selected = {s.harness for s in specs if s.harness}
    found: list[tuple[str, Path]] = []
    for rel, (harness, recognise) in OBSOLETE_FILES.items():
        if harness not in selected:
            continue
        dest = safe_dest(target, rel)
        if dest.is_file() and recognise(dest.read_text(encoding="utf-8", errors="replace")):
            found.append((rel, dest))
    return found


# Shown in a dry run when no hub URL is configured; never written.
HUB_URL_UNSET = "<unset: pass --hub-url or set UA_HTTP_HUB_URL>"


class DistroError(Exception):
    """A refusal with an actionable message (bad input or unsafe target)."""


# --- rendering ------------------------------------------------------------------


def select_specs(harnesses: list[str]) -> list[FileSpec]:
    wanted = set(harnesses)
    return [s for s in FILE_SPECS if s.harness is None or s.harness in wanted]


def render(template_name: str, values: dict[str, str]) -> str:
    text = (template_dir() / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = _PLACEHOLDER_RX.search(text)
    if leftover:
        raise DistroError(f"template {template_name} has an unfilled {leftover.group(0)}")
    return text


def render_values(profile: str, hub_url: str) -> dict[str, str]:
    return {"profile": profile, "hub_url": hub_url, "hub_url_json": json.dumps(hub_url)}


def validate_profile(profile: str) -> str:
    if not _PROFILE_RX.match(profile):
        raise DistroError(
            f"invalid operating profile name {profile!r}: use lowercase letters, digits, "
            "'-' or '_' (at most 64 characters)"
        )
    return profile


def validate_hub_url(url: str, source: str) -> str:
    """Refuse anything but a plain http(s) URL; the error never echoes the value."""
    plain = bool(_HUB_URL_RX.match(url))
    if plain:
        try:
            parts = urlsplit(url)
            plain = bool(parts.hostname) and not (
                parts.username or parts.password or parts.query or parts.fragment
            )
        except ValueError:  # e.g. a malformed IPv6 literal such as http://[bad]/mcp
            plain = False
    if not plain:
        raise DistroError(
            f"hub URL from {source} must be a plain http(s) URL with a host and no "
            "credentials (user:password@), query (?), fragment (#), quotes, spaces or '$'"
        )
    return url


def resolve_hub_url(explicit: str | None) -> tuple[str | None, str]:
    """Return ``(url, source)``, or ``(None, "unset")`` when nothing names the hub.

    There is deliberately no loopback fallback: a generated client must point
    at the hub the operator named, never at whatever listens locally.
    """
    if explicit is not None:
        return validate_hub_url(explicit.strip(), "--hub-url"), "--hub-url"
    env = os.environ.get("UA_HTTP_HUB_URL", "").strip()
    if env:
        return validate_hub_url(env, "UA_HTTP_HUB_URL"), "UA_HTTP_HUB_URL"
    return None, "unset"


def needs_hub_url(specs: list[FileSpec]) -> bool:
    return any(
        "{{hub_url" in (template_dir() / s.template).read_text(encoding="utf-8") for s in specs
    )


def parse_harnesses(raw: list[str] | None) -> list[str]:
    if not raw:
        return list(HARNESSES)
    chosen: list[str] = []
    for chunk in raw:
        for name in chunk.split(","):
            name = name.strip().lower()
            if not name:
                continue
            if name == "all":
                return list(HARNESSES)
            if name not in HARNESSES:
                raise DistroError(
                    f"unknown harness {name!r}; choose from {', '.join(HARNESSES)} or all"
                )
            if name not in chosen:
                chosen.append(name)
    if not chosen:
        raise DistroError("--harness named no harness")
    return chosen


# --- path safety ----------------------------------------------------------------


def resolve_target(path: str) -> Path:
    target = Path(path).expanduser()
    if not target.exists():
        raise DistroError(f"{target} does not exist; point at an existing project directory")
    if not target.is_dir():
        raise DistroError(f"{target} is not a directory")
    return target.resolve()


def safe_dest(target: Path, rel: str) -> Path:
    """Return ``target/rel``, refusing any path that could land outside ``target``.

    ``rel`` must be relative with no ``..``; no existing component under
    ``target`` may be a symlink (a symlinked ``.cursor`` could redirect a write
    anywhere), and the result must resolve inside ``target``.
    """
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise DistroError(f"refusing unsafe relative path {rel!r}")
    current = target
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise DistroError(
                f"refusing to write through symlink {current} (it could point outside "
                f"{target}); replace it with a real directory or file"
            )
    resolved = current.resolve()
    if not resolved.is_relative_to(target):
        raise DistroError(f"refusing {rel!r}: it resolves outside {target}")
    return current


# --- plan -----------------------------------------------------------------------


@dataclass
class PlanItem:
    spec: FileSpec
    path: Path
    content: str
    # create | unchanged | chmod | conflict (differs; --force replaces it) |
    # blocked (a parent or the destination is not a regular file; never replaced)
    status: str
    reason: str = ""


def _blocking_path(target: Path, dest: Path) -> Path | None:
    """The first existing component of ``dest`` that cannot hold the file, if any."""
    rel_parts = dest.relative_to(target).parts
    current = target
    for part in rel_parts[:-1]:
        current = current / part
        if current.exists() and not current.is_dir():
            return current
    if dest.exists() and not dest.is_file():
        return dest
    return None


# --- merging into existing MCP config files ---------------------------------------

_TOML_OUR_HEADER_RX = re.compile(
    r"""^\s*\[\s*mcp_servers\s*\.\s*(?:"unified-agent"|'unified-agent'|unified-agent)"""
    r"""\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$"""
)
_TOML_ANY_HEADER_RX = re.compile(r"^\s*\[")


def _servers(spec: FileSpec, text: str) -> tuple[dict, dict]:
    """Parse ``text``; return ``(document, servers table)``. Raises ValueError."""
    if spec.merge == "toml":
        data = tomllib.loads(text)  # TOMLDecodeError is a ValueError
        key = "mcp_servers"
    else:
        data = json.loads(text)
        key = (spec.merge or "").split(":", 1)[1]
        if not isinstance(data, dict):
            raise ValueError("its top level is not a JSON object")
    servers = data.get(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"its {key!r} is not a table of servers")
    return data, servers


def _entry(spec: FileSpec, text: str):
    return _servers(spec, text)[1].get(SERVER_NAME)


def _without_entry(spec: FileSpec, text: str) -> dict:
    data, servers = _servers(spec, text)
    data = dict(data)
    key = "mcp_servers" if spec.merge == "toml" else (spec.merge or "").split(":", 1)[1]
    rest = {k: v for k, v in servers.items() if k != SERVER_NAME}
    # An empty server table is the same as none: a merge may create the table
    # in a file that had no servers yet without that counting as drift.
    if rest:
        data[key] = rest
    else:
        data.pop(key, None)
    return data


def merged_text(spec: FileSpec, existing: str, rendered: str) -> str:
    """``existing`` with only its unified-agent entry replaced by ``rendered``'s.

    Raises ValueError when the file cannot be merged without risk, including
    when the result would change anything besides that one entry.
    """
    if spec.merge == "toml":
        kept: list[str] = []
        skipping = False
        for line in existing.splitlines(keepends=True):
            if _TOML_ANY_HEADER_RX.match(line):
                skipping = bool(_TOML_OUR_HEADER_RX.match(line))
            if not skipping:
                kept.append(line)
        head = "".join(kept).rstrip("\n")
        merged = f"{head}\n\n{rendered}" if head else rendered
    else:
        key = (spec.merge or "").split(":", 1)[1]
        data, servers = _servers(spec, existing)
        data = dict(data)
        data[key] = {**servers, SERVER_NAME: _entry(spec, rendered)}
        merged = json.dumps(data, indent=2) + "\n"
    if _entry(spec, merged) != _entry(spec, rendered) or _without_entry(
        spec, merged
    ) != _without_entry(spec, existing):
        raise ValueError("a merge would change more than the unified-agent entry")
    return merged


def build_plan(
    target: Path, specs: list[FileSpec], values: dict[str, str], *, hub_known: bool = True
) -> list[PlanItem]:
    """Classify every destination before anything is written (all-or-nothing).

    With ``hub_known=False`` (a dry run with no hub URL) an existing file that
    embeds the hub URL cannot be compared, so it is reported as ``unknown``.
    """
    plan: list[PlanItem] = []
    for spec in specs:
        dest = safe_dest(target, spec.dest)
        content = render(spec.template, values)
        blocker = _blocking_path(target, dest)
        if blocker is not None:
            kind = "a directory" if blocker.is_dir() else "not a directory"
            rel = blocker.relative_to(target).as_posix()
            plan.append(PlanItem(spec, dest, content, "blocked", f"{rel} is {kind}"))
            continue
        if not dest.exists():
            plan.append(PlanItem(spec, dest, content, "create"))
            continue
        existing = dest.read_text(encoding="utf-8", errors="replace")
        if existing == content:
            status = "chmod" if spec.executable and not os.access(dest, os.X_OK) else "unchanged"
            plan.append(PlanItem(spec, dest, content, status))
        elif not hub_known and _uses_hub_url(spec):
            plan.append(PlanItem(spec, dest, content, "unknown", "hub URL unset; cannot compare"))
        elif spec.merge:
            try:
                if _entry(spec, existing) == _entry(spec, content):
                    plan.append(PlanItem(spec, dest, existing, "unchanged"))
                else:
                    plan.append(
                        PlanItem(
                            spec,
                            dest,
                            merged_text(spec, existing, content),
                            "conflict",
                            "--force replaces only its unified-agent entry",
                        )
                    )
            except ValueError as exc:
                plan.append(
                    PlanItem(
                        spec,
                        dest,
                        content,
                        "blocked",
                        f"cannot merge the unified-agent entry ({exc}); edit it by hand",
                    )
                )
        else:
            plan.append(PlanItem(spec, dest, content, "conflict"))
    for rel, dest in _obsolete_matches(target, specs):
        spec = FileSpec(rel, "", None)
        plan.append(PlanItem(spec, dest, "", "remove", "generated by an earlier version"))
    return plan


def _make_parents(target: Path, directory: Path, created: list[Path]) -> None:
    current = target
    for part in directory.relative_to(target).parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise DistroError(
                    f"{current.relative_to(target).as_posix()} changed under us and is no "
                    "longer a plain directory; nothing was written"
                ) from None
            continue
        created.append(current)


def _discard(paths: list[Path], dirs: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for directory in reversed(dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


def _stage(directory: Path, name: str, suffix: str, data: bytes, mode: int) -> Path:
    """Create a unique ``O_EXCL`` temp file in ``directory`` holding ``data``."""
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tmp.chmod(mode)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def apply_plan(target: Path, plan: list[PlanItem]) -> None:
    """Write the plan all-or-nothing, with rollback.

    1. Stage: every new file is written to a unique temp file created with
       ``O_EXCL`` (``tempfile.mkstemp``) in its destination directory, so a
       planted symlink at a predictable name is never followed. A file that
       will be replaced also gets a staged copy of its original bytes and mode.
       A failure here removes everything staged and any directory this run
       created; the project is unchanged.
    2. Commit: staged files are moved into place one by one. If a move fails,
       the files already moved are rolled back (originals restored, new files
       removed) before the error is reported.
    """
    # (new file or None for a removal, original copy, item)
    staged: list[tuple[Path | None, Path | None, PlanItem]] = []
    created_dirs: list[Path] = []
    current = None
    try:
        for item in plan:
            if item.status not in ("create", "conflict", "remove"):
                continue
            current = item
            # Re-check right before writing: the tree may have changed since planning.
            safe_dest(target, item.spec.dest)
            _make_parents(target, item.path.parent, created_dirs)
            backup = None
            if item.path.exists():
                original = item.path.read_bytes()
                if item.status == "remove" and not _obsolete_recogniser(item.spec.dest)(
                    original.decode("utf-8", errors="replace")
                ):
                    # Re-checked on the bytes being backed up: the file changed
                    # since planning and may now be the user's own.
                    raise DistroError(
                        f"{item.spec.dest} changed since it was planned for removal and is "
                        "no longer the file an earlier version generated; nothing was written"
                    )
                backup = _stage(
                    item.path.parent,
                    item.path.name,
                    ".distro-orig",
                    original,
                    item.path.stat().st_mode & 0o7777,
                )
            if item.status == "remove":
                if backup is not None:
                    staged.append((None, backup, item))
                continue
            try:
                new = _stage(
                    item.path.parent,
                    item.path.name,
                    ".distro-tmp",
                    item.content.encode("utf-8"),
                    0o755 if item.spec.executable else 0o644,
                )
            except OSError:
                if backup is not None:
                    backup.unlink(missing_ok=True)
                raise
            staged.append((new, backup, item))
    except (OSError, DistroError) as exc:
        _discard([p for new, bak, _ in staged for p in (new, bak) if p], created_dirs)
        if isinstance(exc, DistroError):
            raise
        where = current.spec.dest if current else "?"
        raise DistroError(
            f"could not stage {where}: {exc.strerror or exc}; nothing was written"
        ) from None

    committed: list[tuple[Path | None, Path | None, PlanItem]] = []
    for entry in staged:
        new, _backup, item = entry
        try:
            if new is None:  # an obsolete file: its backup is already staged
                if _backup is None or item.path.read_bytes() != _backup.read_bytes():
                    raise OSError(0, "changed after it was staged for removal", str(item.path))
                item.path.unlink()
            else:
                os.replace(new, item.path)
        except OSError as exc:
            unrestored = _rollback(committed)
            # A backup that could not be put back is the only copy of the
            # original: never discard it.
            keep = {backup for _dest, backup in unrestored}
            _discard(
                [p for n, b, _ in staged for p in (n, b) if p and p not in keep and p.exists()],
                created_dirs,
            )
            if unrestored:
                kept = "; ".join(
                    f"{dest} (original kept at {backup})" if backup else f"{dest} (new file left)"
                    for dest, backup in unrestored
                )
                raise DistroError(
                    f"could not move {item.spec.dest} into place ({exc.strerror or exc}), "
                    f"and rollback failed for: {kept}. Restore those by hand."
                ) from None
            raise DistroError(
                f"could not move {item.spec.dest} into place ({exc.strerror or exc}); "
                "rolled back, nothing was changed"
            ) from None
        committed.append(entry)
    _discard([b for _n, b, _i in staged if b], [])
    for item in plan:
        if item.status == "chmod":
            item.path.chmod(0o755)


def _rollback(
    committed: list[tuple[Path | None, Path | None, PlanItem]],
) -> list[tuple[str, Path | None]]:
    """Undo committed moves, newest first.

    Returns ``(dest, backup)`` for every move that could not be undone; the
    caller must keep each such backup, since it holds the only original copy.
    """
    failed: list[tuple[str, Path | None]] = []
    for _new, backup, item in reversed(committed):
        try:
            if backup is not None:
                os.replace(backup, item.path)
            else:
                item.path.unlink(missing_ok=True)
        except OSError:
            failed.append((item.spec.dest, backup))
    return failed


# --- check ----------------------------------------------------------------------


@dataclass
class Issue:
    kind: str  # missing | out-of-date | not-executable | secret | instruction | shadow
    path: str
    detail: str


def _walk_header_env_values(node, out: list[tuple[str, str]], trail: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if key in ("headers", "env", "environment", "http_headers") and isinstance(value, dict):
                for k, v in value.items():
                    if isinstance(v, str):
                        out.append((f"{here}.{k}", v))
            elif key in ("bearer_token", "token", "api_key") and isinstance(value, str):
                out.append((here, value))
            else:
                _walk_header_env_values(value, out, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk_header_env_values(value, out, f"{trail}[{i}]")


def scan_secrets(rel: str, text: str) -> list[Issue]:
    """Literal-secret findings for one file; messages carry masked values only."""
    findings = list(check_secrets(rel, text))
    issues = [Issue("secret", rel, f"line {f.line}: {f.message}") for f in findings]
    flagged_lines = {f.line for f in findings}
    for i, line in enumerate(text.splitlines(), 1):
        if i in flagged_lines:
            continue
        m = _HUB_TOKEN_RX.search(line)
        if m:
            issues.append(Issue("secret", rel, f"line {i}: literal hub token {_mask(m.group(0))}"))
            continue
        if BEARER_RX.search(line):
            token = _literal_mcp_secret(line.strip().strip(",").strip('"'))
            if token:
                issues.append(Issue("secret", rel, f"line {i}: literal bearer {_mask(token)}"))
    parsed = None
    if rel.endswith(".json"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    elif rel.endswith(".toml"):
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            parsed = None
    if parsed is not None:
        values: list[tuple[str, str]] = []
        _walk_header_env_values(parsed, values)
        for where, value in values:
            token = _literal_mcp_secret(value)
            if token and not any(token in iss.detail for iss in issues):
                issues.append(Issue("secret", rel, f"{where}: literal secret {_mask(token)}"))
    return issues


def _shadow_issue(target: Path) -> Issue | None:
    mcp = target / ".mcp.json"
    if not mcp.is_file() or mcp.is_symlink():
        return None
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict) and any("unified-agent" in str(k) for k in servers):
        return Issue(
            "shadow",
            ".mcp.json",
            "project-scoped unified-agent entry shadows the user-scope hub registration; "
            "remove it and use .unified-agent/bin/ua-register-claude",
        )
    return None


def _uses_hub_url(spec: FileSpec) -> bool:
    return needs_hub_url([spec])


def run_check(
    target: Path, specs: list[FileSpec], values: dict[str, str], *, hub_known: bool = True
) -> list[Issue]:
    """Report problems. With ``hub_known=False`` files that embed the hub URL
    are still checked for presence and secrets, but not for drift."""
    issues: list[Issue] = []
    scanned: set[str] = set()
    for spec in specs:
        dest = safe_dest(target, spec.dest)
        if not dest.is_file():
            issues.append(Issue("missing", spec.dest, "run `unified-agent-distro init --write`"))
            continue
        text = dest.read_text(encoding="utf-8", errors="replace")
        scanned.add(spec.dest)
        if hub_known or not _uses_hub_url(spec):
            rendered = render(spec.template, values)
            if spec.merge and text != rendered:
                # Other servers may share the file; only our entry has to match.
                try:
                    if _entry(spec, text) != _entry(spec, rendered):
                        issues.append(
                            Issue("out-of-date", spec.dest, "unified-agent entry differs")
                        )
                except ValueError as exc:
                    issues.append(Issue("unparsable", spec.dest, str(exc)))
            elif text != rendered:
                issues.append(
                    Issue("out-of-date", spec.dest, "differs from the generated text (edited?)")
                )
        if spec.executable and not os.access(dest, os.X_OK):
            issues.append(Issue("not-executable", spec.dest, "chmod +x it"))
        issues.extend(scan_secrets(spec.dest, text))
        if spec.dest in INSTRUCTION_FILES:
            for finding in (
                *check_hidden_unicode(spec.dest, text),
                *check_risky_instructions(spec.dest, text),
            ):
                issues.append(Issue("instruction", spec.dest, finding.message))
    for rel in _EXTRA_SCAN_PATHS:
        if rel in scanned:
            continue
        try:
            dest = safe_dest(target, rel)
        except DistroError:
            continue
        if dest.is_file():
            scanned.add(rel)
            issues.extend(scan_secrets(rel, dest.read_text(encoding="utf-8", errors="replace")))
    for rel, _dest in _obsolete_matches(target, specs):
        issues.append(
            Issue(
                "obsolete",
                rel,
                "generated by an earlier version; `unified-agent-distro init --write` removes it",
            )
        )
    shadow = _shadow_issue(target)
    if shadow:
        issues.append(shadow)
    return issues


# --- CLI ------------------------------------------------------------------------


def _existing_profile(target: Path) -> str | None:
    try:
        path = safe_dest(target, ".unified-agent/operating.yaml")
    except DistroError:
        return None
    if not path.is_file():
        return None
    m = _OPERATING_PROFILE_RX.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def _dev_root_warning(target: Path, harnesses: list[str]) -> str | None:
    """A warning when project-scoped hub config would land under ~/dev."""
    if not _PROJECT_HUB_HARNESSES.intersection(harnesses):
        return None
    try:
        dev = (Path.home() / "dev").resolve()
    except (OSError, RuntimeError):
        return None
    if target != dev and not target.is_relative_to(dev):
        return None
    return (
        f"{target} is under {dev}: project-scoped unified-agent entries for "
        "Codex, Cursor, OpenCode and Copilot there would shadow the fleet's user-scope "
        "hub registration."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unified-agent-distro",
        description="Drop a portable unified-agent setup (constitution, per-harness hub "
        "config, operating profile, starter skill) into a project, or check one.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("path", help="Existing project directory")
        p.add_argument(
            "--harness",
            action="append",
            metavar="NAME",
            help=f"Limit to these harnesses (repeat or comma-separate; default all: "
            f"{', '.join(HARNESSES)})",
        )
        p.add_argument(
            "--hub-url",
            help="Hub MCP URL (default: UA_HTTP_HUB_URL; there is no local fallback, so "
            "init --write refuses when neither is set)",
        )
        p.add_argument(
            "--profile",
            help="Operating profile name (default: the name already in "
            f".unified-agent/operating.yaml, else {DEFAULT_PROFILE})",
        )

    init_p = sub.add_parser("init", help="Plan (default) or write the distro files")
    common(init_p)
    init_p.add_argument("--write", action="store_true", help="Apply the plan")
    init_p.add_argument(
        "--force",
        action="store_true",
        help="With --write, replace files whose content differs. In existing MCP config "
        "files (.codex/config.toml, .cursor/mcp.json, opencode.json, .vscode/mcp.json) only "
        "the unified-agent entry is replaced; other servers are kept",
    )
    init_p.add_argument(
        "--allow-dev-root",
        action="store_true",
        help="Allow --write under ~/dev, where a project-scoped hub entry shadows the "
        "fleet's user-scope registration",
    )

    check_p = sub.add_parser("check", help="Report missing or drifted files and literal secrets")
    common(check_p)
    return parser


def _cmd_init(args: argparse.Namespace, target: Path, harnesses: list[str]) -> int:
    profile = validate_profile(args.profile or _existing_profile(target) or DEFAULT_PROFILE)
    hub_url, source = resolve_hub_url(args.hub_url)
    specs = select_specs(harnesses)
    dev_root = _dev_root_warning(target, harnesses)
    if dev_root and args.write and not args.allow_dev_root:
        raise DistroError(f"{dev_root} Re-run with --allow-dev-root if this is intended.")
    if hub_url is None and args.write and needs_hub_url(specs):
        raise DistroError(
            "no hub URL: pass --hub-url or set UA_HTTP_HUB_URL to the hub's MCP URL "
            "(there is no local default, so generated clients never reach the wrong hub)"
        )
    plan = build_plan(
        target,
        specs,
        render_values(profile, hub_url or HUB_URL_UNSET),
        hub_known=hub_url is not None,
    )
    conflicts = [i for i in plan if i.status == "conflict"]
    blocked = [i for i in plan if i.status == "blocked"]

    mode = "write" if args.write else "dry run"
    print(f"unified-agent-distro init ({mode}): {target}")
    print(f"  harnesses: {', '.join(harnesses)}")
    print(f"  operating profile: {profile}")
    if hub_url is None:
        print(f"  hub URL: {HUB_URL_UNSET} (required for --write)")
    else:
        print(f"  hub URL: {hub_url} (from {source})")
    if dev_root:
        print(f"  warning: {dev_root}", file=sys.stderr)
    for item in plan:
        label = item.status
        if item.status == "conflict":
            label = "overwrite" if args.force else "CONFLICT"
        elif item.status == "blocked":
            label = "BLOCKED"
        elif item.status == "unknown":
            label = "unknown"
        suffix = f" ({item.reason})" if item.reason else ""
        print(f"  {label:<10} {item.spec.dest}{suffix}")

    if blocked:
        print(
            f"\n{len(blocked)} destination(s) are blocked (see the reason for each); "
            "nothing was written and --force does not override this. Fix or move them "
            "aside, then re-run:",
            file=sys.stderr,
        )
        for item in blocked:
            print(f"  {item.spec.dest}: {item.reason}", file=sys.stderr)
        return 1

    if conflicts and not args.force:
        print(
            f"\n{len(conflicts)} file(s) exist with different content; refusing to "
            "overwrite. Review them, then re-run with --force to replace them:",
            file=sys.stderr,
        )
        for item in conflicts:
            suffix = f" ({item.reason})" if item.reason else ""
            print(f"  {item.spec.dest}{suffix}", file=sys.stderr)
        return 1

    pending = [i for i in plan if i.status not in ("unchanged", "unknown")]
    if not args.write:
        if pending:
            print(f"\nDry run: {len(pending)} change(s). Re-run with --write to apply.")
        else:
            print("\nNothing to do: every file is up to date.")
        return 0

    apply_plan(target, plan)
    print(f"\nWrote {len(pending)} file(s); {len(plan) - len(pending)} already up to date.")
    if "claude" in harnesses:
        print(
            "Next: run .unified-agent/bin/ua-register-claude once per machine to register "
            "the hub with Claude Code at user scope."
        )
    return 0


def _cmd_check(args: argparse.Namespace, target: Path, harnesses: list[str]) -> int:
    profile = validate_profile(args.profile or _existing_profile(target) or DEFAULT_PROFILE)
    hub_url, _source = resolve_hub_url(args.hub_url)
    issues = run_check(
        target,
        select_specs(harnesses),
        render_values(profile, hub_url or HUB_URL_UNSET),
        hub_known=hub_url is not None,
    )
    print(f"unified-agent-distro check: {target}")
    if hub_url is None:
        print(
            "  note: hub URL unset (pass --hub-url or set UA_HTTP_HUB_URL); files that "
            "embed it were checked for presence and secrets, not drift"
        )
    if not issues:
        print("  ok: every managed file is present, current and free of literal secrets")
        return 0
    for issue in issues:
        print(f"  {issue.kind:<14} {issue.path}: {issue.detail}")
    print(f"\n{len(issues)} issue(s).")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        harnesses = parse_harnesses(args.harness)
        target = resolve_target(args.path)
        if args.command == "init":
            return _cmd_init(args, target, harnesses)
        return _cmd_check(args, target, harnesses)
    except DistroError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        where = exc.filename or "a project file"
        print(f"error: cannot access {where}: {exc.strerror or exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
