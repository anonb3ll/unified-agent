"""Skills registry: org-wide skill versions, per-agent/profile assignment,
dispatch injection, and per-surface drift reporting (task #1353, Parity 6).

Gap this closes: skills were hand-synced per surface (the cockpit's
``~/agent-workspace/.claude/skills`` is tracked in git; ``~/.claude/skills``,
``~/.agents/skills`` etc. are per-machine links with no shared source of
truth). ``skill_review`` (``tools/skills.py``) already lints a SKILL.md for
quality; this module is the registry kernel that sits above it: immutable
versions gated by that lint, assignment to an agent label or a profile, a
pure resolver dispatch can inject from, and a read-only drift report a surface
directory can be checked against.

Kept deliberately small — task #1424 builds an installable catalog UI on top
of this registry and is expected to consume :func:`list_skills`, :func:`get`,
and :func:`resolve` as a stable reader API. Kernel version operations never
write a surface directory; catalog actions delegate to the governed installer
in skill_catalog. :func:`drift` only reads.

Versions are immutable and monotonically increasing per name. Publishing the
same content again is idempotent: it returns the existing version rather than
minting a new one, keyed on the sha256 content hash. Assignment points a
skill name (optionally pinned to a version, else "whatever is latest when
resolved") at an agent label or a profile name; an agent-level assignment
takes precedence over a profile-level one when both exist for the same skill.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import unicodedata
from collections.abc import Callable
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, storage, table_schemas
from .safety import resolve_for_audit
from .tools.skills import KEBAB_RE, skill_review

NAME_MAX = 64
TARGET_TYPES = ("agent", "profile")

# Dispatch injection is best-effort and bounded: a misconfigured registry
# must never grow the prompt unboundedly or take down a run.
MAX_RESOLVED_SKILLS = 20
CONTENT_CAP = 4000
INJECTION_TOTAL_CAP = 8000
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_DRIFT_SKILLS = 100
MAX_DRIFT_ENTRIES = 10_000
MAX_PACKAGE_FILES = 128
MAX_PACKAGE_ENTRIES = 512
MAX_PACKAGE_FILE_BYTES = 512 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_PATH = 240
MAX_PACKAGE_DEPTH = 8
MAX_PACKAGE_REFERENCES = 4096
PACKAGE_SCHEMA_VERSION = 1
_LOCAL_FILE_START = re.compile(r"(?<![\w/:-])(?:scripts|references|assets|resources)/")

# A mutation must publish its event before another registry mutation can
# commit. Otherwise concurrent callers can notify the event bus in an order
# that disagrees with durable ULID order, causing subscribers to discard an
# earlier event after advancing their cursor.
_MUTATION_LOCK = threading.RLock()


def _ordered_mutation(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _MUTATION_LOCK:
            return fn(*args, **kwargs)

    return wrapped


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the skills registry tables. The DDL lives in core
    (``table_schemas.skills_registry``), which ``storage.init_db`` runs."""
    table_schemas.skills_registry(conn)


def _author(author: str | None) -> str:
    identity = config.verified_agent_identity()
    if identity:
        return identity
    text = (author or "").strip()
    if not text:
        raise ToolError("author is required when no verified identity is present")
    return text


def _public(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys() if key != "package_payload"}
    if data.get("package_manifest") is not None:
        data["package_manifest"] = json.loads(data["package_manifest"])
    return data


def _validate_name(name: str) -> str:
    text = (name or "").strip()
    if not text or not KEBAB_RE.match(text) or len(text) > NAME_MAX:
        raise ToolError(
            f"skill name {name!r} must be kebab-case (lowercase letters, digits, "
            f"hyphens), at most {NAME_MAX} chars"
        )
    return text


def _validate_target_type(target_type: str) -> str:
    value = (target_type or "").strip().lower()
    if value not in TARGET_TYPES:
        raise ToolError(f"target_type must be one of {', '.join(TARGET_TYPES)}")
    return value


def _metadata_public(row: sqlite3.Row) -> dict[str, Any]:
    """``_public`` minus the full ``SKILL.md`` body — what a publish response returns.

    ``get`` (the reader API #1424 builds on) still returns ``content`` in
    full; a publish caller already has the source on disk and does not need
    it echoed back across the trust boundary.
    """
    data = _public(row)
    data.pop("content", None)
    return data


def _read_bounded_skill(path: Path, operation: str) -> str:
    """Read no more than the registry's per-skill body limit."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SKILL_FILE_BYTES + 1)
    except OSError as exc:
        raise ToolError("SKILL.md could not be read") from exc
    if len(raw) > MAX_SKILL_FILE_BYTES:
        raise ToolError(f"SKILL.md exceeds the {MAX_SKILL_FILE_BYTES}-byte {operation} limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("SKILL.md must be valid UTF-8") from exc


def _single_skill_md_path(resolved: Path, original: str) -> Path:
    """The one ``SKILL.md`` a publish targets — never a collection root.

    Unlike ``skill_review``, which accepts a directory of skill directories
    and reviews all of them, ``publish`` versions exactly one skill: ``path``
    must be a ``SKILL.md`` file or a single skill directory (one that has its
    own ``SKILL.md``).
    """
    if resolved.is_file():
        if resolved.name != "SKILL.md":
            raise ToolError(f"{original!r} is a file but not a SKILL.md")
        return resolved
    if resolved.is_dir():
        md = resolved / "SKILL.md"
        if md.is_file():
            return md
        raise ToolError(
            f"{original!r} has no SKILL.md at its root; publish takes one skill "
            "directory or SKILL.md file, not a collection of skill directories "
            "(skill_review accepts a collection root for linting; publish does not)"
        )
    raise ToolError(f"not found: {original!r}")


def _validate_publish_root(resolved: Path, original: str) -> None:
    """Keep publication sources out of broad read-only audit roots.

    Audit roots can contain sensitive material scanned by security tools. A
    registry source must instead be under the process workspace or an
    operator-configured ``UA_SKILL_REGISTRY_ROOTS`` directory.
    """
    roots = [config.process_workspace_root().resolve()]
    roots.extend(
        Path(value).expanduser().resolve()
        for value in os.environ.get("UA_SKILL_REGISTRY_ROOTS", "").split(os.pathsep)
        if value.strip()
    )
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ToolError(
            f"publish source {original!r} is outside the workspace and configured "
            "UA_SKILL_REGISTRY_ROOTS; UA_AUDIT_ROOTS and the vault are read-only "
            "audit sources, not publish sources"
        )


def _frontmatter_name(text: str) -> str | None:
    """The skill's own ``name:`` frontmatter field, or ``None`` if unreadable.

    ``skill_review`` already checks that this matches the skill's *directory*
    name; publish separately checks it matches the registry ``name`` it is
    being published under, so a copy-pasted or renamed skill cannot silently
    land under the wrong registry entry.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        meta = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    name = meta.get("name") if isinstance(meta, dict) else None
    return name if isinstance(name, str) else None


def _package_path(value: str) -> str:
    """Canonical relative member names only; no hidden/runtime/credential paths."""
    if not isinstance(value, str) or len(value) > MAX_PACKAGE_PATH:
        raise ToolError("unsafe supporting file path")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) > MAX_PACKAGE_DEPTH
        or any(part in {".", ".."} or part.startswith(".") for part in path.parts)
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ToolError("unsafe supporting file path")
    return value


def _package_manifest(members: dict[str, tuple[bytes, int]]) -> dict:
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(members.items())
        ],
    }


def _package_hash(manifest: dict) -> str:
    # Keep the existing SKILL.md-only API hash; multi-file hashes attest every member.
    if len(manifest["files"]) == 1 and manifest["files"][0]["path"] == "SKILL.md":
        return manifest["files"][0]["sha256"]
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _capture_package(
    root: Path, operation: str, *, path_guard=None, directory_paths=None
) -> dict[str, tuple[bytes, int]]:
    """Bounded byte snapshot. Symlinks and special files never enter a version."""
    from .safety import guard_secret_filename

    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ToolError("skill package refuses symlink sources")
    if not root.is_dir():
        raise ToolError("skill package directory is unavailable")
    members = {}
    portable_names = set()
    todo = [root]
    total = count = 0
    while todo:
        directory = todo.pop()
        with os.scandir(directory) as children:
            for entry in children:
                count += 1
                if count > MAX_PACKAGE_ENTRIES:
                    raise ToolError("skill package exceeds its entry work limit")
                candidate = Path(entry.path)
                relative = _package_path(candidate.relative_to(root).as_posix())
                portable = unicodedata.normalize("NFC", relative).casefold()
                if portable in portable_names:
                    raise ToolError("skill package refuses casefold/Unicode path collisions")
                portable_names.add(portable)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ToolError("skill package refuses symlink members")
                if path_guard is not None and path_guard(candidate) != candidate:
                    raise ToolError("skill package member escaped its scoped root")
                guard_secret_filename(candidate, relative)
                if stat.S_ISDIR(info.st_mode):
                    if directory_paths is not None:
                        directory_paths.add(relative)
                    todo.append(candidate)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ToolError("skill package refuses special files")
                cap = MAX_SKILL_FILE_BYTES if relative == "SKILL.md" else MAX_PACKAGE_FILE_BYTES
                # O_NOFOLLOW closes the final-component replacement window.
                fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise ToolError("skill package refuses special files")
                    data = handle.read(cap + 1)
                if len(data) > cap:
                    label = "SKILL.md" if relative == "SKILL.md" else "supporting file"
                    raise ToolError(f"{label} exceeds the {cap}-byte {operation} limit")
                total += len(data)
                if len(members) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise ToolError("skill package exceeds its file or byte limit")
                members[relative] = (
                    data,
                    0o755 if relative != "SKILL.md" and opened.st_mode & 0o111 else 0o644,
                )
    return members


def _package_text_references(body: str):
    """Use CommonMark context for destinations; check bare paths in all text.

    Input bytes/files and reference occurrences are bounded by the package
    contract. The parser limits nesting and owns code, link-title, label,
    image and reference-link context; malformed prose cannot mask later links.
    """
    from markdown_it import MarkdownIt
    from markdown_it.parser_block import ParserBlock
    from markdown_it.parser_inline import ParserInline

    # The canonical parser normally drops the remaining input when a nesting
    # limit is reached. Package verification must refuse that input instead:
    # a skipped paragraph can contain an otherwise rendered local destination.
    class PackageBlockParser(ParserBlock):
        def tokenize(self, state, startLine, endLine):
            if state.level >= state.md.options["maxNesting"]:
                raise ToolError("skill package exceeds its Markdown nesting limit")
            return super().tokenize(state, startLine, endLine)

    class PackageInlineParser(ParserInline):
        def __init__(self):
            super().__init__()
            self.depth = 0

        def parse(self, src, md, env, tokens):
            if self.depth >= md.options["maxNesting"]:
                raise ToolError("skill package exceeds its Markdown nesting limit")
            self.depth += 1
            try:
                return super().parse(src, md, env, tokens)
            finally:
                self.depth -= 1

        def tokenize(self, state):
            if state.level >= state.md.options["maxNesting"]:
                raise ToolError("skill package exceeds its Markdown nesting limit")
            return super().tokenize(state)

        def skipToken(self, state):
            if state.level >= state.md.options["maxNesting"]:
                raise ToolError("skill package exceeds its Markdown nesting limit")
            return super().skipToken(state)

    def bare(fragment, *, literal=False):
        position = 0
        while match := _LOCAL_FILE_START.search(fragment, position):
            start = match.start()
            end = match.end()
            depth = 0
            quoted = literal or (start > 0 and fragment[start - 1] in {"`", '"'})
            # Scan forward once. Greedily capturing the remaining fragment and
            # trimming each adjacent mention would re-scan it quadratically.
            while end < len(fragment):
                char = fragment[end]
                if char.isspace() or char in '`"<>':
                    break
                if char == "\\":
                    end += 2
                    continue
                if not quoted:
                    if char == "[":
                        depth += 1
                    elif char == "]":
                        if depth == 0:
                            break
                        depth -= 1
                end += 1
            yield fragment[start:end].rstrip(".,;)")
            # Resume after the delimiter; adjacent mentions each receive
            # validation and cap accounting, with filename brackets preserved.
            position = end + (end < len(fragment))

    parser = MarkdownIt(
        "commonmark", {"maxNesting": 20, "inline_definitions": True, "store_labels": True}
    )
    parser.block = PackageBlockParser()
    parser.inline = PackageInlineParser()
    parser.configure(
        "commonmark", {"maxNesting": 20, "inline_definitions": True, "store_labels": True}
    )
    # Markdown's renderer normally suppresses schemes such as file:. Parse
    # them as links so our stricter package URI/path authority can refuse them.
    parser.validateLink = lambda _href: True

    tokens = parser.parse(body)

    def descendants(tokens):
        for token in tokens:
            yield token
            if token.children:
                yield from descendants(token.children)

    used_definitions = {
        token.meta["label"]
        for token in descendants(tokens)
        if token.type in {"link_open", "image"} and "label" in token.meta
    }
    seen_definitions = set()

    def references(tokens):
        for token in tokens:
            if token.type == "definition":
                # CommonMark normally omits definitions from its token stream.
                # Retain originals (including shadowed duplicates) so their
                # labels and unused destinations/titles cannot hide bare paths.
                yield from bare(token.meta["label"])
                definition = token.meta["id"]
                used = definition in used_definitions and definition not in seen_definitions
                seen_definitions.add(definition)
                # Referenced definitions already contribute their destination
                # and title at each actual link/image occurrence below. Avoid
                # charging the first definition a second time; duplicate or
                # unused definitions still receive independent validation.
                if not used:
                    destination = token.meta["url"]
                    if not destination.startswith("#"):
                        yield destination
                    yield from bare(token.meta["title"])
            elif token.type in {"link_open", "image"}:
                destination = token.attrGet("href" if token.type == "link_open" else "src")
                if destination is not None and not destination.startswith("#"):
                    yield destination
                title = token.attrGet("title")
                if title:
                    yield from bare(title)
            elif token.type in {
                "text",
                "code_inline",
                "code_block",
                "fence",
                "html_inline",
                "html_block",
            }:
                yield from bare(
                    token.content, literal=token.type in {"code_inline", "code_block", "fence"}
                )
                if token.type == "fence":
                    yield from bare(token.info)
            if token.children:
                yield from references(token.children)

    yield from references(tokens)


def _check_package_references(text: str, members: dict) -> None:
    """Check every captured Markdown document within the bounded package."""
    documents = {"SKILL.md": text}
    documents.update(
        (path, data.decode("utf-8", errors="replace"))
        for path, (data, _) in members.items()
        if path != "SKILL.md" and PurePosixPath(path).suffix.casefold() in {".md", ".markdown"}
    )
    count = 0
    for document, body in documents.items():
        for reference in _package_text_references(body):
            count += 1
            if count > MAX_PACKAGE_REFERENCES:
                raise ToolError("skill package exceeds its supporting reference work limit")
            if not reference:
                raise ToolError("unsafe supporting file path")
            reference = reference.split()[0]
            # Markdown escapes syntax punctuation before URL decoding. Encoded
            # backslashes remain paths and are still refused by _package_path.
            reference = re.sub(r"\\([\[\]()])", r"\1", reference)
            parsed = urlsplit(reference)
            if reference.startswith("#") or parsed.scheme in {"https", "http", "mailto"}:
                continue
            relative = _package_path(unquote(parsed.path).rstrip("/"))
            if parsed.scheme or parsed.netloc:
                raise ToolError("unsafe supporting file path")
            path = _package_path((PurePosixPath(document).parent / relative).as_posix())
            if path not in members and not any(p.startswith(path + "/") for p in members):
                raise ToolError(f"missing supporting file: {path!r}")


def _audit_package(members: dict) -> list[dict]:
    from .audit.rules import CONTENT_RULES, check_private_key, check_secrets

    findings = []
    for path, (data, mode) in sorted(members.items()):
        text = data.decode("utf-8", errors="replace")
        # Secret rules normally skip lockfiles/dotenv. A package publishes all
        # bytes, so credential screening uses an unskippable synthetic filename.
        if next(check_secrets("skill-package-payload", text), None) or next(
            check_private_key(path, text), None
        ):
            raise ToolError("skill package contains credential material and cannot be published")
        if b"\x00" in data and (mode & 0o111 or not path.startswith("assets/")):
            raise ToolError("binary supporting files must be non-executable assets")
        for rule in CONTENT_RULES:
            for finding in rule(path, text) or ():
                if len(findings) >= 500:
                    raise ToolError("skill package audit exceeds its finding limit")
                findings.append(finding.to_dict())
    return findings


def _verify_package_row(row) -> dict[str, tuple[bytes, int]]:
    """Verify persisted attestation without reading or trusting the mutable source."""
    if row["package_manifest"] is None:
        if hashlib.sha256(row["content"].encode()).hexdigest() != row["content_hash"]:
            raise ToolError("skill content hash does not match the reviewed registry version")
        return {"SKILL.md": (row["content"].encode(), 0o644)}
    try:
        raw = row["package_payload"]
        if not isinstance(raw, str) or len(raw) > MAX_PACKAGE_BYTES * 2:
            raise ValueError("payload limit")
        if len(row["package_manifest"]) > MAX_PACKAGE_FILES * 1024:
            raise ValueError("manifest limit")
        manifest = json.loads(row["package_manifest"])
        payload = json.loads(raw)
        if manifest["schema_version"] != PACKAGE_SCHEMA_VERSION or not isinstance(payload, dict):
            raise ValueError("schema")
        members = {}
        total = 0
        for item in manifest["files"]:
            path = _package_path(item["path"])
            if (
                path in members
                or item["mode"] not in {0o644, 0o755}
                or (path == "SKILL.md" and item["mode"] != 0o644)
            ):
                raise ValueError("member")
            data = base64.b64decode(payload[path], validate=True)
            cap = MAX_SKILL_FILE_BYTES if path == "SKILL.md" else MAX_PACKAGE_FILE_BYTES
            total += len(data)
            if len(data) > cap or len(members) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise ValueError("bounds")
            members[path] = (data, item["mode"])
        if set(payload) != set(members) or "SKILL.md" not in members:
            raise ValueError("member set")
        if manifest != _package_manifest(members) or _package_hash(manifest) != row["content_hash"]:
            raise ValueError("hash")
        if members["SKILL.md"][0] != row["content"].encode():
            raise ValueError("instructions")
        _check_package_references(row["content"], members)
        return members
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError("skill package attestation/hash verification failed") from exc


def version_files(name: str, version: int) -> dict[str, tuple[bytes, int]]:
    """Installer-only immutable bytes; public readers never echo binary payloads."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM skill_versions WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            raise ToolError("unknown skill package version")
        return _verify_package_row(row)
    finally:
        conn.close()


@_ordered_mutation
def publish(name: str, path: str, author: str | None = None) -> dict[str, Any]:
    """Publish a new immutable version, gated by ``skill_review``.

    Refuses (returns ``ok: False`` with the review's findings, does not
    raise) when the lint finds any blocking (``high`` severity) issue.
    Re-publishing identical content is idempotent: the existing version for
    that content hash is returned rather than minting a new one. ``path``
    must resolve to exactly one skill (a ``SKILL.md`` file or a directory
    that is itself a skill) — a collection root is rejected. Raises if the
    skill's own frontmatter ``name`` does not match ``name``.
    """
    skill_name = _validate_name(name)
    who = _author(author)
    resolved = resolve_for_audit(path)
    _validate_publish_root(resolved, path)
    md_path = _single_skill_md_path(resolved, path)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = config.process_workspace_root() / candidate
    for ancestor in (candidate, *candidate.parents):
        if ancestor.is_symlink():
            raise ToolError("skill package refuses symlink sources")
    package_root = md_path.parent
    members = _capture_package(
        package_root, "publish", path_guard=lambda p: resolve_for_audit(str(p))
    )
    text = _read_bounded_skill(md_path, "publish")
    if members.get("SKILL.md", (None,))[0] != text.encode():
        raise ToolError("skill package changed while it was being reviewed; retry publish")
    _check_package_references(text, members)
    support_findings = _audit_package(members)
    review = _merge_package_findings(skill_review(path), support_findings)
    if review["status"] == "fail":
        return {"ok": False, "blocked": True, "name": skill_name, "review": review}
    if (
        _capture_package(package_root, "publish", path_guard=lambda p: resolve_for_audit(str(p)))
        != members
    ):
        raise ToolError("skill package changed while it was being reviewed; retry publish")
    fm_name = _frontmatter_name(text)
    if fm_name is not None and fm_name != skill_name:
        raise ToolError(
            f"publish name {skill_name!r} does not match the SKILL.md frontmatter "
            f"name {fm_name!r} at {path!r}; publish under {fm_name!r} or fix the "
            "frontmatter"
        )
    return _store_version(skill_name, members, text, str(resolved), who, review)


def _merge_package_findings(review: dict, support_findings: list[dict]) -> dict:
    """Fold supporting-file audit findings into a ``skill_review`` result."""
    review["package_findings"] = support_findings
    review["findings"].extend(support_findings)
    if "summary" in review:
        for finding in support_findings:
            severity = finding["severity"]
            review["summary"][severity] += 1
    if any(f["severity"] == "high" for f in support_findings):
        review["status"] = "fail"
    elif any(f["severity"] == "medium" for f in support_findings) and review["status"] == "pass":
        review["status"] = "warn"
    return review


def _store_version(
    skill_name: str,
    members: dict[str, tuple[bytes, int]],
    text: str,
    source: str,
    who: str,
    review: dict,
    event_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert one reviewed, immutable version (or return the identical one).

    Shared by local ``publish`` and the git skill-source importer (#1750);
    ``source`` is the provenance string stored on the version row.
    """
    manifest = _package_manifest(members)
    content_hash = _package_hash(manifest)
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    payload_json = json.dumps(
        {p: base64.b64encode(data).decode("ascii") for p, (data, _) in sorted(members.items())},
        separators=(",", ":"),
    )

    conn = storage.connect()
    publish_payload: dict[str, Any] | None = None
    try:
        # BEGIN IMMEDIATE takes the write lock before the MAX(version) read
        # below, so two concurrent publishers of the same name cannot both
        # compute the same "next" version — the second blocks (busy_timeout)
        # until the first's transaction ends, then re-reads a fresh MAX.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM skill_versions WHERE name=? AND content_hash=? AND package_manifest IS NOT NULL "
            "ORDER BY version DESC LIMIT 1",
            (skill_name, content_hash),
        ).fetchone()
        if existing is not None:
            _verify_package_row(existing)
            conn.rollback()
            return {
                "ok": True,
                "idempotent": True,
                "skill": _metadata_public(existing),
                "review": review,
            }
        row = conn.execute(
            "SELECT MAX(version) AS v FROM skill_versions WHERE name=?", (skill_name,)
        ).fetchone()
        next_version = int(row["v"]) + 1 if row and row["v"] is not None else 1
        now = storage.now()
        try:
            conn.execute(
                "INSERT INTO skill_versions (name, version, content_hash, source, content, "
                "published_by, published_at, package_manifest, package_payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    skill_name,
                    next_version,
                    content_hash,
                    source,
                    text,
                    who,
                    now,
                    manifest_json,
                    payload_json,
                ),
            )
        except sqlite3.IntegrityError:
            # Defensive fallback — BEGIN IMMEDIATE above should make this
            # unreachable under normal concurrency, but if some other writer
            # still won the (name, version) row, re-check idempotency before
            # giving up: identical content published concurrently is still a
            # no-op, not an error.
            conn.rollback()
            again = conn.execute(
                "SELECT * FROM skill_versions WHERE name=? AND content_hash=? AND package_manifest IS NOT NULL "
                "ORDER BY version DESC LIMIT 1",
                (skill_name, content_hash),
            ).fetchone()
            if again is not None:
                return {
                    "ok": True,
                    "idempotent": True,
                    "skill": _metadata_public(again),
                    "review": review,
                }
            raise
        # Audit append lands in the same transaction as the version insert
        # (config_revisions.py's pattern for audited writes, the strongest of
        # the three in this codebase — goals.py/task_comments.py record after
        # commit, so a record_event failure there leaves a durable mutation
        # with no audit row and an error surfaced to a caller whose write
        # actually succeeded). Atomic here: either both land, or neither does.
        _, _, publish_payload = events.record_event_once_on_connection(
            conn,
            "skill.published",
            payload={
                "name": skill_name,
                "version": next_version,
                "content_hash": content_hash,
                "package_manifest": manifest,
                "source": source,
                "published_at": now,
                **(event_extra or {}),
            },
            originator=who,
        )
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM skill_versions WHERE name=? AND version=?",
            (skill_name, next_version),
        ).fetchone()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return {"ok": True, "idempotent": False, "skill": _metadata_public(stored), "review": review}  # type: ignore[arg-type]


def get(name: str, version: int | None = None) -> dict[str, Any] | None:
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise ToolError("version must be a positive integer")
    conn = storage.connect()
    try:
        if version is not None:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if len(row["content"].encode()) > MAX_SKILL_FILE_BYTES:
        raise ToolError(f"stored SKILL.md exceeds the {MAX_SKILL_FILE_BYTES}-byte get limit")
    _verify_package_row(row)
    return _public(row)


# ``list`` omits the stored SKILL.md body; ``get`` returns one version in full.
_LIST_COLUMNS = (
    "id, name, version, content_hash, source, published_by, published_at, package_manifest"
)


def list_skills(name: str | None = None) -> dict[str, Any]:
    """List every version (all skills, or one name's history), newest first per name."""
    conn = storage.connect()
    try:
        if name:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM skill_versions WHERE name=? "
                "ORDER BY name, version DESC",
                (name,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM skill_versions ORDER BY name, version DESC"
            ).fetchall()
    finally:
        conn.close()
    return {"skills": [_public(r) for r in rows]}


@_ordered_mutation
def assign(
    skill_name: str,
    target: str,
    target_type: str = "agent",
    version: int | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Point ``skill_name`` (optionally pinned to ``version``) at an agent label or profile."""
    who = _author(author)
    name = _validate_name(skill_name)
    ttype = _validate_target_type(target_type)
    who_target = (target or "").strip()
    if not who_target:
        raise ToolError("target is required")
    if ttype == "profile":
        who_target = _normalize_profile_target(who_target)
    pinned_version = version
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ToolError("version must be a positive integer")
    conn = storage.connect()
    try:
        if pinned_version is not None:
            row = conn.execute(
                "SELECT 1 FROM skill_versions WHERE name=? AND version=?",
                (name, pinned_version),
            ).fetchone()
            if row is None:
                raise ToolError(f"unknown version {version} for skill {name!r}")
        else:
            row = conn.execute(
                "SELECT 1 FROM skill_versions WHERE name=? LIMIT 1", (name,)
            ).fetchone()
            if row is None:
                raise ToolError(f"skill {name!r} has no published version yet")
        now = storage.now()
        conn.execute(
            "INSERT INTO skill_assignments (skill_name, target_type, target, pinned_version, "
            "assigned_by, assigned_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(skill_name, target_type, target) DO UPDATE SET "
            "pinned_version=excluded.pinned_version, assigned_by=excluded.assigned_by, "
            "assigned_at=excluded.assigned_at",
            (name, ttype, who_target, pinned_version, who, now),
        )
        # Same atomic-with-the-write pattern as publish (see its comment):
        # the audit event lands in this transaction, not after conn.commit().
        _, _, publish_payload = events.record_event_once_on_connection(
            conn,
            "skill.assigned",
            payload={
                "name": name,
                "target_type": ttype,
                "target": who_target,
                "version": pinned_version,
            },
            originator=who,
        )
        # Materialize the response before commit releases SQLite's write lock;
        # another process could otherwise update/delete this assignment first.
        stored = conn.execute(
            "SELECT * FROM skill_assignments WHERE skill_name=? AND target_type=? AND target=?",
            (name, ttype, who_target),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return {"assignment": _public(stored)}  # type: ignore[arg-type]


@_ordered_mutation
def unassign(
    skill_name: str, target: str, target_type: str = "agent", author: str | None = None
) -> dict[str, Any]:
    who = _author(author)
    name = _validate_name(skill_name)
    ttype = _validate_target_type(target_type)
    who_target = (target or "").strip()
    if not who_target:
        raise ToolError("target is required")
    if ttype == "profile":
        # Unassignment is idempotent and must also clean up rows for a profile
        # that has since been retired. Canonicalize the key without revalidating it.
        who_target = who_target.lower()
    conn = storage.connect()
    publish_payload: dict[str, Any] | None = None
    try:
        cur = conn.execute(
            "DELETE FROM skill_assignments WHERE skill_name=? AND target_type=? AND target=?",
            (name, ttype, who_target),
        )
        removed = cur.rowcount > 0
        if removed:
            _, _, publish_payload = events.record_event_once_on_connection(
                conn,
                "skill.unassigned",
                payload={"name": name, "target_type": ttype, "target": who_target},
                originator=who,
            )
        conn.commit()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return {"removed": removed}


def _normalize_profile_target(target: str) -> str:
    from . import profiles

    normalized = target.strip().lower()
    if normalized not in profiles.VALID_PROFILES:
        raise ToolError(
            f"unknown profile {target!r}; choose from {', '.join(sorted(profiles.VALID_PROFILES))}"
        )
    return normalized


def resolve(agent_label: str, profile: str | None = None) -> dict[str, Any]:
    """Pure reader: the skills assigned to ``agent_label`` (directly, or via its profile).

    Best-effort and bounded — this is called from the dispatch hot path, so it
    never raises: any failure (empty registry, missing tables in an older DB,
    a bad profile lookup) degrades to an empty skill list rather than
    propagating. An agent-level assignment for a given skill name wins over a
    profile-level one for that same name.
    """
    label = (agent_label or "").strip()
    if not label:
        return {"agent_label": agent_label, "profile": profile, "skills": []}
    try:
        prof = profile
        if prof is None:
            try:
                from . import profiles

                prof = profiles.default_for_identity(label)
            except Exception:
                prof = None
        conn = storage.connect()
        try:
            # Direct agent assignments are the strongest match and must have
            # deterministic priority when the resolver's bounded result is
            # full. Sort each source before merging, then keep direct rows
            # first; direct assignments also win duplicate names.
            profile_rows = list(
                conn.execute(
                    "SELECT * FROM skill_assignments WHERE target_type='profile' AND target=? "
                    "ORDER BY skill_name",
                    (prof,),
                ).fetchall()
                if prof
                else []
            )
            agent_rows = conn.execute(
                "SELECT * FROM skill_assignments WHERE target_type='agent' AND target=? "
                "ORDER BY skill_name",
                (label,),
            ).fetchall()
            by_name: dict[str, sqlite3.Row] = {}
            for row in agent_rows:
                by_name[row["skill_name"]] = row
            for row in profile_rows:
                by_name.setdefault(row["skill_name"], row)
            skills: list[dict[str, Any]] = []
            for skill_name, assignment in list(by_name.items())[:MAX_RESOLVED_SKILLS]:
                pinned = assignment["pinned_version"]
                if pinned is not None:
                    vrow = conn.execute(
                        "SELECT * FROM skill_versions WHERE name=? AND version=?",
                        (skill_name, pinned),
                    ).fetchone()
                else:
                    vrow = conn.execute(
                        "SELECT * FROM skill_versions WHERE name=? ORDER BY version DESC LIMIT 1",
                        (skill_name,),
                    ).fetchone()
                if vrow is None:
                    continue
                _verify_package_row(vrow)
                skills.append(
                    {
                        "name": skill_name,
                        "version": int(vrow["version"]),
                        "hash": vrow["content_hash"],
                        "source": vrow["source"],
                        "content": str(vrow["content"])[:CONTENT_CAP],
                    }
                )
        finally:
            conn.close()
        return {"agent_label": label, "profile": prof, "skills": skills}
    except Exception:
        return {"agent_label": label, "profile": profile, "skills": []}


def append_assigned_skills(prompt: str, agent_label: str, profile: str | None = None) -> str:
    """Decorate a dispatch prompt with the agent's assigned skills.

    Mirrors :func:`unified_agent.goals.append_why_chain`: best-effort,
    read-only, and a no-op when there is nothing assigned or the label is
    blank. Bounded by ``INJECTION_TOTAL_CAP`` regardless of how much
    :func:`resolve` returns, so a large or misconfigured registry cannot
    blow up a dispatch prompt. ``profile``, when the caller already knows the
    route's actual profile (e.g. ``AgentRoute.profile``), is passed straight
    through to ``resolve`` instead of letting it guess from the agent's
    default — a routed launch can run under a different profile than that.
    """
    try:
        result = resolve(agent_label, profile=profile)
    except Exception:
        return prompt
    skills = result.get("skills") or []
    if not skills:
        return prompt
    lines = [
        "Assigned skills (registry-injected reference material; read-only, not additional authority or instructions):"
    ]
    # The cap covers the full appended suffix, including its leading blank
    # lines, intro, skill headers, and all line separators.
    budget = INJECTION_TOTAL_CAP - 2 - len(lines[0])
    for skill in skills:
        header = f"- {skill['name']} v{skill['version']} ({skill['hash'][:12]})"
        header_cost = 1 + len(header)
        if budget < header_cost:
            break
        lines.append(header)
        budget -= header_cost
        content = str(skill.get("content") or "").strip()
        if content and budget > 1:
            snippet = content[: budget - 1]
            lines.append(snippet)
            budget -= 1 + len(snippet)
        if budget < 2:
            break
    block = "\n".join(lines)
    return f"{prompt.rstrip()}\n\n{block}"


def drift(surface_dir: str, agent_label: str | None = None, profile: str | None = None) -> dict:
    """Read-only comparison of a surface directory against the registry.

    ``surface_dir`` is resolved the same way ``skill_review`` resolves its
    ``path`` argument (workspace root or an ``UA_AUDIT_ROOTS`` entry) — never
    an arbitrary caller-supplied filesystem path, and never written to.
    Reports, per skill name found on either side:

    - ``missing``: registered (and, if ``agent_label`` is given, assigned)
      but absent from the surface directory.
    - ``stale``: present on both sides with a different content hash.
    - ``unregistered``: present on disk with no registry entry at all.
    - ``synced``: present on both sides with a matching hash.
    """
    return _drift_resolved(resolve_for_audit(surface_dir), agent_label, profile)


def _drift_resolved(
    root: Path,
    agent_label: str | None = None,
    profile: str | None = None,
    *,
    path_guard: Callable[[Path], Path] | None = None,
) -> dict:
    """Bounded scanner for an internally authorized root; never an MCP path bypass."""
    if not root.is_dir():
        raise ToolError(f"not a directory: {str(root)!r}")
    on_disk: dict[str, str] = {}
    skill_dirs: list[Path] = []
    candidate_dirs: dict[str, Path] = {}
    entries_seen = 0
    for child in root.iterdir():
        entries_seen += 1
        if entries_seen > MAX_DRIFT_ENTRIES:
            raise ToolError(f"drift scan exceeds the {MAX_DRIFT_ENTRIES}-entry work limit")
        if child.is_dir():
            candidate_dirs[child.name] = child
        if child.is_dir() and (child / "SKILL.md").is_file():
            skill_dirs.append(child)
            if len(skill_dirs) > MAX_DRIFT_SKILLS:
                raise ToolError(f"drift scan exceeds the {MAX_DRIFT_SKILLS}-skill directory limit")
    skill_dirs.sort()
    for child in skill_dirs:
        md = child / "SKILL.md"
        if path_guard is not None:
            md = path_guard(md)
        body = _read_bounded_skill(md, "drift scan")
        on_disk[child.name] = hashlib.sha256(body.encode()).hexdigest()

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT sv.* FROM skill_versions sv "
            "JOIN (SELECT name, MAX(version) AS version FROM skill_versions GROUP BY name) latest "
            "ON sv.name = latest.name AND sv.version = latest.version"
        ).fetchall()
        all_latest = {row["name"]: _public(row) for row in rows}
        latest = all_latest
    finally:
        conn.close()

    assigned_names: set[str] | None = None
    if agent_label:
        # Compare against what dispatch would actually inject, which honours a
        # pinned version, rather than against the newest published version.
        resolved = resolve(agent_label, profile=profile)["skills"]
        assigned_names = {s["name"] for s in resolved}
        # In scoped mode the comparison side is exactly what dispatch resolves.
        # Keep the all-registry map separately to classify installed but
        # unassigned skills accurately instead of calling them unregistered.
        latest = {}
        for skill in resolved:
            latest[skill["name"]] = {"version": skill["version"], "content_hash": skill["hash"]}

    names = set(on_disk) | set(latest) | (assigned_names or set())
    missing: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    unregistered: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    synced: list[dict[str, Any]] = []
    captured_skills = len(skill_dirs)
    for name in sorted(names):
        disk_hash = on_disk.get(name)
        reg = latest.get(name)
        full = get(name, reg["version"]) if reg is not None else None
        if full and full.get("package_manifest") is not None and name in candidate_dirs:
            if name not in on_disk:
                captured_skills += 1
                if captured_skills > MAX_DRIFT_SKILLS:
                    raise ToolError(
                        f"drift scan exceeds the {MAX_DRIFT_SKILLS}-skill directory limit"
                    )
            members = _capture_package(candidate_dirs[name], "drift scan", path_guard=path_guard)
            disk_hash = _package_hash(_package_manifest(members))
        if disk_hash is None:
            if reg is not None and (assigned_names is None or name in assigned_names):
                missing.append({"name": name, "registry_version": reg["version"]})
            continue
        if reg is None:
            if assigned_names is not None and name in all_latest:
                unassigned.append(
                    {
                        "name": name,
                        "on_disk_hash": disk_hash,
                        "registry_version": all_latest[name]["version"],
                    }
                )
            else:
                unregistered.append({"name": name, "on_disk_hash": disk_hash})
            continue
        if disk_hash != reg["content_hash"]:
            stale.append(
                {
                    "name": name,
                    "on_disk_hash": disk_hash,
                    "registry_hash": reg["content_hash"],
                    "registry_version": reg["version"],
                }
            )
        else:
            synced.append({"name": name, "version": reg["version"]})
    return {
        "root": str(root),
        "comparison": "resolved_assignments" if agent_label else "latest_published",
        "missing": missing,
        "stale": stale,
        "unregistered": unregistered,
        "unassigned": unassigned,
        "synced": synced,
    }


def skill_registry(action: str, params: dict | None = None) -> dict[str, Any]:
    """Registry operations and catalog installs, including starter_preview/starter_install.

    Starter actions bind a reviewed team template to explicit roster seats and
    existing operating/trust profiles. Installation uses configuration revision
    authority and an exact-policy-base human approval; no permissions are granted.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    name = action.strip().lower()
    body = params or {}
    if not isinstance(body, dict):
        raise ToolError("params must be an object")
    if name in {"starter_preview", "starter_install"}:
        from . import team_templates

        allowed = {
            "template_id",
            "team_id",
            "content_hash",
            "bindings",
            "operating_profile",
            "trust_preset",
        }
        if set(body) != allowed:
            raise ToolError("starter actions require exactly " + ", ".join(sorted(allowed)))
        return (
            team_templates.preview(**body)
            if name == "starter_preview"
            else team_templates.install(**body)
        )
    if name in {"catalog", "install", "team_install"}:
        from . import skill_catalog

        if name == "catalog":
            return skill_catalog.catalog(surface_dir=body.get("surface_dir"))
        if name == "install":
            return skill_catalog.install(
                name=str(body.get("name") or ""),
                version=body.get("version"),
                content_hash=body.get("content_hash"),
                surface_dir=body.get("surface_dir"),
                expected_hash=body.get("expected_hash"),
            )
        return skill_catalog.install_team(
            team_id=body.get("team_id"),
            revision=body.get("revision"),
            content_hash=body.get("content_hash"),
        )
    if name.startswith("source_"):
        return _source_action(name, body)
    if name == "publish":
        if body.get("catalog_only"):
            from . import skill_catalog

            return skill_catalog.publish(
                name=str(body.get("name") or ""), path=str(body.get("path") or "")
            )
        return publish(
            name=str(body.get("name") or ""),
            path=str(body.get("path") or ""),
            author=body.get("author"),
        )
    if name == "list":
        return list_skills(name=body.get("name"))
    if name == "get":
        detail = get(str(body.get("name") or ""), version=body.get("version"))
        if detail is None:
            raise ToolError(f"unknown skill: {body.get('name')!r}")
        return detail
    if name == "assign":
        return assign(
            skill_name=str(body.get("skill_name") or body.get("name") or ""),
            target=str(body.get("target") or ""),
            target_type=str(body.get("target_type") or "agent"),
            version=body.get("version"),
            author=body.get("author"),
        )
    if name == "unassign":
        return unassign(
            skill_name=str(body.get("skill_name") or body.get("name") or ""),
            target=str(body.get("target") or ""),
            target_type=str(body.get("target_type") or "agent"),
            author=body.get("author"),
        )
    if name == "resolve":
        return resolve(agent_label=str(body.get("agent_label") or ""), profile=body.get("profile"))
    if name == "drift":
        return drift(
            surface_dir=str(body.get("surface_dir") or ""),
            agent_label=body.get("agent_label"),
            profile=body.get("profile"),
        )
    raise ToolError(
        "action must be one of publish, list, get, assign, unassign, resolve, drift, catalog, "
        "install, team_install, source_add, source_list, source_discover, source_refresh, "
        "source_import"
    )


def _source_action(name: str, body: dict) -> dict[str, Any]:
    """Git-pinned skill sources (task #1750); see ``skill_sources``."""
    from . import skill_sources

    if name == "source_add":
        return skill_sources.add(
            url=str(body.get("url") or ""),
            ref=str(body.get("ref") or "HEAD"),
            author=body.get("author"),
        )
    if name == "source_list":
        return skill_sources.list_sources()
    if name == "source_discover":
        return skill_sources.discover(body.get("source_id"), sha=body.get("sha"))
    if name == "source_refresh":
        return skill_sources.refresh(body.get("source_id"), author=body.get("author"))
    if name == "source_import":
        return skill_sources.import_package(
            body.get("source_id"),
            str(body.get("path") or ""),
            name=body.get("name"),
            sha=body.get("sha"),
            author=body.get("author"),
        )
    raise ToolError(
        "source action must be one of source_add, source_list, source_discover, "
        "source_refresh, source_import"
    )
