"""Git-pinned skill sources for the skills registry (task #1750, Parity 26).

``skills_registry.publish`` versions a skill from a local path. This module
adds a second, provenance-carrying way in: register a Git repository and ref,
pin the ref to an exact 40-character commit SHA, list the ``SKILL.md``
packages in that commit, and import one as an immutable registry version
whose ``source`` is ``git:<url>@<sha>:<path>``.

Safety model:

- **Read-only, no execution.** The repository is fetched into a private bare
  cache (``$UA_HOME/skill_sources/<hash>.git``, created with an empty
  template, so it has no hooks directory) and read only with plumbing
  (``ls-tree``, ``cat-file``, ``diff-tree``). Nothing is ever checked out, so
  no filter, hook, installer or script in the repository can run. Every git
  call also forces ``core.hooksPath=/dev/null`` and ``core.fsmonitor=false``
  so a user-level config cannot run code either.
- **Exact pins.** A source stores the commit SHA its ref resolved to.
  ``refresh`` re-resolves the ref and reports what changed, but never moves
  the pin or imports; only an explicit ``import`` at the refreshed SHA does.
- **Bounded packages.** A package is the directory holding a ``SKILL.md``.
  It may not contain another ``SKILL.md`` package, symlinks, submodules,
  hidden or secret-named members, or unsafe paths, and it obeys the same
  file, byte and count limits as a locally published package. The import then
  runs the ``skill_review`` lint and the supporting-file audit exactly as
  ``publish`` does.
- **Schemes.** ``https://``, ``ssh://git@`` and ``git@host:path`` only;
  ``file://`` is refused unless ``UA_SKILL_SOURCES_ALLOW_FILE=1`` (tests and
  local mirrors). Credentials are whatever git and ``gh`` already have; a URL
  carrying a username or password is refused so no secret lands in the DB.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, skills_registry, storage
from .tools.skills import review_skill_dir

FILE_URL_ENV = "UA_SKILL_SOURCES_ALLOW_FILE"
SHA_RE = re.compile(r"[0-9a-f]{40}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_SCP_RE = re.compile(r"git@([A-Za-z0-9][A-Za-z0-9.-]*):([A-Za-z0-9._~][A-Za-z0-9._~/-]*)")
MAX_URL = 512
MAX_REF = 200
MAX_LOCATION = 400
MAX_TREE_ENTRIES = 50_000
MAX_TREE_OUTPUT = 16 * 1024 * 1024
MAX_DISCOVERED = 200
MAX_DIFF_FILES = 200
MAX_DESCRIPTION = 300
GIT_TIMEOUT = 120

# Hardening applied to every git invocation. ``-c`` beats user and system
# config, so a global hooksPath, fsmonitor or submodule setting cannot run code.
_GIT_HARDENING = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
    "-c",
    "fetch.recurseSubmodules=false",
    "-c",
    "credential.interactive=never",
)
# Inherited git variables that only select credentials or config files. Every
# other GIT_* (GIT_DIR, GIT_CONFIG_PARAMETERS, object-directory overrides...)
# is dropped so the hub's environment cannot redirect the cache.
_KEEP_GIT_ENV = frozenset(
    {"GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
)
_CACHE_LOCK = threading.Lock()


# --- validation ---------------------------------------------------------------


def _file_urls_allowed() -> bool:
    return os.environ.get(FILE_URL_ENV, "").strip() == "1"


def validate_url(url: str) -> str:
    """Return ``url`` if it is an allowed, credential-free remote, else raise."""
    text = (url or "").strip() if isinstance(url, str) else ""
    if (
        not text
        or len(text) > MAX_URL
        or text.startswith("-")
        or any(ord(c) <= 32 or ord(c) == 127 for c in text)
    ):
        raise ToolError("skill source url must be a non-empty URL without spaces")
    if text.startswith(("https://", "ssh://", "file://")):
        parts = urlsplit(text)
        if parts.query or parts.fragment:
            raise ToolError("skill source url must not carry a query or fragment")
        if parts.scheme == "file":
            if not _file_urls_allowed():
                raise ToolError(
                    f"file:// skill sources are disabled; set {FILE_URL_ENV}=1 "
                    "(tests and local mirrors only)"
                )
            if parts.netloc or not parts.path.startswith("/"):
                raise ToolError("file:// skill source must be file:///absolute/path")
            return text
        host = parts.hostname or ""
        if not _HOST_RE.fullmatch(host) or not parts.path.strip("/"):
            raise ToolError("skill source url needs a host and a repository path")
        if parts.password is not None:
            raise ToolError(
                "skill source url must not embed credentials; git and gh credential "
                "helpers supply them"
            )
        if parts.scheme == "https" and parts.username is not None:
            raise ToolError(
                "skill source url must not embed credentials; git and gh credential "
                "helpers supply them"
            )
        if parts.scheme == "ssh" and parts.username != "git":
            raise ToolError("ssh skill sources must use the git user (ssh://git@host/path)")
        return text
    match = _SCP_RE.fullmatch(text)
    if match and ".." not in match.group(2).split("/"):
        return text
    raise ToolError(
        "skill source url must be https://, ssh://git@host/path or git@host:path "
        f"(file:// only when {FILE_URL_ENV}=1)"
    )


def _validate_ref(ref: str) -> str:
    text = (ref or "").strip() if isinstance(ref, str) else ""
    if SHA_RE.fullmatch(text):
        return text
    if (
        not text
        or len(text) > MAX_REF
        or not _REF_RE.fullmatch(text)
        or ".." in text
        or "//" in text
        or text.endswith((".", "/", ".lock"))
        or any(part.startswith(".") for part in text.split("/"))
    ):
        raise ToolError(
            "ref must be a branch or tag name (letters, digits, '.', '_', '-', '/') "
            "or an exact 40-character commit SHA"
        )
    return text


def _validate_sha(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not SHA_RE.fullmatch(text):
        raise ToolError("sha must be an exact 40-character lowercase commit SHA")
    return text


def _location(path: Any) -> str:
    """Normalize a package directory inside the repository ('' is the root)."""
    if not isinstance(path, str):
        raise ToolError("path must be a string")
    text = path.strip()
    if text.startswith("/"):
        raise ToolError("absolute package paths are refused; use a repository-relative path")
    text = text.rstrip("/")
    if text in {"", "."}:
        return ""
    if len(text) > MAX_LOCATION or "\\" in text or any(ord(c) < 32 for c in text):
        raise ToolError("unsafe package path")
    parts = text.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise ToolError("unsafe package path: '.', '..', '.git' and empty segments are refused")
    return text


# --- git plumbing ----------------------------------------------------------------


def cache_dir(url: str) -> Path:
    """The private bare cache for ``url``."""
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return config.data_home() / "skill_sources" / f"{digest}.git"


def _protocol(url: str) -> str:
    if url.startswith("file://"):
        return "file"
    if url.startswith("https://"):
        return "https"
    return "ssh"


def _git_env(url: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in _KEEP_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PROTOCOL_FROM_USER"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = _protocol(url) if url else "none"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["LC_ALL"] = "C"
    return env


def _git(
    repo: Path | None,
    *args: str,
    url: str | None = None,
    max_output: int = 1024 * 1024,
    check: bool = True,
) -> tuple[int, bytes]:
    """Run one bounded git command; never a shell, never a prompt."""
    cmd = ["git", *_GIT_HARDENING]
    if repo is not None:
        cmd.append(f"--git-dir={repo}")
    cmd.extend(args)
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                env=_git_env(url),
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolError("git is not available on this host") from exc
        timer = threading.Timer(GIT_TIMEOUT, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            out = proc.stdout.read(max_output + 1)
            if len(out) > max_output:
                proc.kill()
                proc.wait()
                raise ToolError("git output exceeds the skill source work limit")
            proc.stdout.close()
            code = proc.wait()
        finally:
            timer.cancel()
        if check and code != 0:
            err.seek(0)
            detail = err.read(600).decode("utf-8", "replace").strip().splitlines()
            hint = detail[-1] if detail else f"exit {code}"
            raise ToolError(f"git {args[0]} failed: {hint}")
    return code, out


@contextlib.contextmanager
def _locked_cache(url: str):
    """Serialize fetches into one cache across threads and processes."""
    path = cache_dir(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with _CACHE_LOCK, open(path.with_suffix(".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            if not (path / "HEAD").is_file():
                _git(None, "init", "--quiet", "--bare", "--template=", str(path))
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _fetch(url: str, cache: Path, ref: str) -> str:
    """Fetch ``ref`` shallowly into the cache and return its commit SHA."""
    dst = f"refs/ua/fetch/{hashlib.sha256(ref.encode()).hexdigest()[:16]}"
    _git(
        cache,
        "fetch",
        "--quiet",
        "--depth=1",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--",
        url,
        f"+{ref}:{dst}",
        url=url,
    )
    _, out = _git(cache, "rev-parse", "--verify", "--quiet", f"{dst}^{{commit}}")
    sha = out.decode().strip()
    if not SHA_RE.fullmatch(sha):
        raise ToolError(f"ref {ref!r} did not resolve to a commit")
    # Keep every SHA we have pinned or reported reachable, so gc never drops it.
    _git(cache, "update-ref", f"refs/ua/sha/{sha}", sha)
    return sha


def _ensure_commit(url: str, sha: str) -> Path:
    with _locked_cache(url) as cache:
        code, _ = _git(cache, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
        if code != 0:
            if _fetch(url, cache, sha) != sha:
                raise ToolError("fetched commit does not match the pinned SHA")
        return cache


def _tree(cache: Path, sha: str) -> list[tuple[str, str, str, int | None, str]]:
    """(mode, type, oid, size, path) for every entry of the commit, bounded."""
    _, out = _git(
        cache, "ls-tree", "-r", "-l", "-z", "--full-tree", sha, max_output=MAX_TREE_OUTPUT
    )
    entries = []
    for record in out.split(b"\0"):
        if not record:
            continue
        if len(entries) >= MAX_TREE_ENTRIES:
            raise ToolError(f"repository tree exceeds the {MAX_TREE_ENTRIES}-entry discovery limit")
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, oid, size = meta.decode().split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            path = raw_path.decode("utf-8", "replace")
            kind = "invalid-path"
        entries.append((mode, kind, oid, None if size == "-" else int(size), path))
    return entries


def _location_problem(location: str) -> str | None:
    try:
        if _location(location) != location:
            return "unsafe package path"
    except ToolError as exc:
        return str(exc)
    return None


def _member_problem(rel: str, mode: str, kind: str) -> str | None:
    from .safety import _PROJECT_READ_SECRET_RX

    if kind == "invalid-path":
        return f"member path is not valid UTF-8: {rel!r}"
    if mode == "120000":
        return f"symlinks are refused in a skill package: {rel!r}"
    if mode == "160000" or kind == "commit":
        return f"submodules are refused in a skill package: {rel!r}"
    if kind != "blob" or mode not in {"100644", "100755"}:
        return f"unsupported tree entry: {rel!r}"
    try:
        skills_registry._package_path(rel)
    except ToolError:
        return f"unsafe supporting file path (hidden, '..', ':' or too deep): {rel!r}"
    if any(_PROJECT_READ_SECRET_RX.search(part) for part in PurePosixPath(rel).parts):
        return f"secret-bearing filename refused: {rel!r}"
    return None


_ROOT = object()  # trie key marking a package root


def _portable(path: str) -> str:
    """Collision key: compatibility-normalized and case-folded."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", path).casefold())


def _entry_count(files: list[tuple[str, str, str, int]]) -> int:
    """Files plus distinct directories, as the installer's capture counts them.

    ``skills_registry._capture_package`` (staged install verification) counts
    every file and directory under the package root against
    ``MAX_PACKAGE_ENTRIES``; an import must not accept what it would refuse.
    """
    entries = set()
    for rel, *_rest in files:
        parts = rel.split("/")
        entries.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return len(entries)


def _collisions(files: list[tuple[str, str, str, int]]) -> list[str]:
    """Members or directories that collide after NFKC + casefold.

    The same rule ``skills_registry._capture_package`` applies at publish and
    install time (it uses NFC; NFKC is strictly broader), so an imported
    version can always be installed. Member paths are already bounded by
    ``_package_path`` (length and depth), so the prefix walk is bounded.
    """
    seen: dict[str, str] = {}
    problems: list[str] = []
    for rel, *_rest in files:
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            exact = "/".join(parts[:depth])
            key = _portable(exact)
            other = seen.setdefault(key, exact)
            if other != exact:
                problems.append(f"case/Unicode path collision refused: {other!r} and {exact!r}")
    return problems


def _packages(entries) -> dict[str, dict[str, Any]]:
    """Every SKILL.md package in a tree, with members and refusal reasons.

    Linear in the tree size: package roots go into a component trie, and each
    entry walks its own path once to find the deepest root that owns it, so a
    tree of many sibling packages never rescans the entry list per root.
    """
    roots = {
        "" if "/" not in path else path.rsplit("/", 1)[0]
        for _m, _k, _o, _s, path in entries
        if path.rsplit("/", 1)[-1] == "SKILL.md"
    }
    trie: dict = {}
    for root in roots:
        node = trie
        for part in root.split("/") if root else ():
            node = node.setdefault(part, {})
        node[_ROOT] = root
    nested: dict[str, list[str]] = {}
    for root in roots:
        if not root:
            continue
        node = trie
        if _ROOT in node:
            nested.setdefault(node[_ROOT], []).append(root)
        parts = root.split("/")
        for part in parts[:-1]:
            node = node[part]
            if _ROOT in node:
                nested.setdefault(node[_ROOT], []).append(root)
    owned: dict[str, list] = {root: [] for root in roots}
    for entry in entries:
        path = entry[4]
        node = trie
        owner = trie.get(_ROOT)
        for part in path.split("/")[:-1]:
            node = node.get(part)
            if node is None:
                break
            owner = node.get(_ROOT, owner)
        if owner is not None:
            owned[owner].append(entry)
    packages: dict[str, dict[str, Any]] = {}
    for root in sorted(roots):
        prefix = f"{root}/" if root else ""
        members = owned[root]
        inner = sorted(nested.get(root, ()))
        problems: list[str] = []
        if location_problem := _location_problem(root):
            problems.append(location_problem)
        if inner:
            problems.append(
                "contains nested SKILL.md package(s) at "
                + ", ".join(inner[:5])
                + "; import those packages individually"
            )
        total = 0
        files = []
        for mode, kind, oid, size, path in members:
            rel = path[len(prefix) :]
            if problem := _member_problem(rel, mode, kind):
                problems.append(problem)
                continue
            cap = (
                skills_registry.MAX_SKILL_FILE_BYTES
                if rel == "SKILL.md"
                else skills_registry.MAX_PACKAGE_FILE_BYTES
            )
            if size is None or size > cap:
                label = "SKILL.md" if rel == "SKILL.md" else f"supporting file {rel!r}"
                problems.append(f"{label} exceeds the {cap}-byte import limit")
                continue
            total += size
            files.append((rel, mode, oid, size))
        problems.extend(_collisions(files))
        if _entry_count(files) > skills_registry.MAX_PACKAGE_ENTRIES:
            problems.append(
                f"package exceeds the installer's {skills_registry.MAX_PACKAGE_ENTRIES}-entry "
                "limit (files plus directories)"
            )
        if len(files) > skills_registry.MAX_PACKAGE_FILES:
            problems.append(
                f"package exceeds the {skills_registry.MAX_PACKAGE_FILES}-file import limit"
            )
        if total > skills_registry.MAX_PACKAGE_BYTES:
            problems.append(
                f"package exceeds the {skills_registry.MAX_PACKAGE_BYTES}-byte import limit"
            )
        fingerprint = hashlib.sha256(
            "\n".join(f"{m} {o} {r}" for r, m, o, _s in sorted(files)).encode()
            + "\n".join(problems).encode()
        ).hexdigest()
        skill_md = next((f for f in files if f[0] == "SKILL.md"), None)
        packages[root] = {
            "path": root or ".",
            "files": files,
            "bytes": total,
            "problems": problems,
            "fingerprint": fingerprint,
            "skill_md": skill_md,
        }
    return packages


def _read_blob(cache: Path, oid: str, size: int) -> bytes:
    _, data = _git(cache, "cat-file", "blob", oid, max_output=size)
    if len(data) != size:
        raise ToolError("git blob size changed while it was being read")
    return data


def _frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    try:
        meta = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


# --- persistence ------------------------------------------------------------------


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _source(conn: sqlite3.Connection, source_id: Any) -> sqlite3.Row:
    if isinstance(source_id, bool) or not isinstance(source_id, int):
        try:
            source_id = int(str(source_id))
        except ValueError as exc:
            raise ToolError("source_id must be an integer; see source_list") from exc
    row = conn.execute("SELECT * FROM skill_sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise ToolError(f"unknown skill source {source_id}; see source_list")
    return row


def _load(source_id: Any) -> dict[str, Any]:
    conn = storage.connect()
    try:
        return _public(_source(conn, source_id))
    finally:
        conn.close()


def _select_sha(source: dict[str, Any], sha: Any) -> str:
    if sha is None or sha == "":
        return source["pinned_sha"]
    chosen = _validate_sha(sha)
    if chosen not in {source["pinned_sha"], source["refreshed_sha"]}:
        raise ToolError(
            "sha is neither the pinned nor the last refreshed SHA of this source; "
            "run source_refresh first"
        )
    return chosen


def _record(conn, channel: str, payload: dict, who: str):
    _, _, publish_payload = events.record_event_once_on_connection(
        conn, channel, payload=payload, originator=who
    )
    return publish_payload


# --- operations ------------------------------------------------------------------


def add(url: str, ref: str = "HEAD", author: str | None = None) -> dict[str, Any]:
    """Register ``url`` at ``ref`` and pin it to the commit the ref resolves to now."""
    who = skills_registry._author(author)
    clean_url = validate_url(url)
    clean_ref = _validate_ref(ref or "HEAD")
    conn = storage.connect()
    try:
        existing = conn.execute(
            "SELECT * FROM skill_sources WHERE url=? AND ref=?", (clean_url, clean_ref)
        ).fetchone()
    finally:
        conn.close()
    if existing is not None:
        return {"ok": True, "idempotent": True, "source": _public(existing)}
    with _locked_cache(clean_url) as cache:
        sha = _fetch(clean_url, cache, clean_ref)
    if SHA_RE.fullmatch(clean_ref) and sha != clean_ref:
        raise ToolError("fetched commit does not match the requested SHA")
    now = storage.now()
    conn = storage.connect()
    publish_payload = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO skill_sources (url, ref, pinned_sha, refreshed_sha, "
                "last_refreshed, added_by, added_at) VALUES (?,?,?,?,?,?,?)",
                (clean_url, clean_ref, sha, sha, now, who, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT * FROM skill_sources WHERE url=? AND ref=?", (clean_url, clean_ref)
            ).fetchone()
            return {"ok": True, "idempotent": True, "source": _public(row)}
        publish_payload = _record(
            conn,
            "skill.source_added",
            {"source_id": cur.lastrowid, "url": clean_url, "ref": clean_ref, "sha": sha},
            who,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM skill_sources WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return {"ok": True, "idempotent": False, "source": _public(row)}


def list_sources() -> dict[str, Any]:
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT * FROM skill_sources ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"sources": [_public(r) for r in rows]}


def discover(source_id: Any, sha: Any = None) -> dict[str, Any]:
    """List the SKILL.md packages at the pinned (or last refreshed) SHA. Read-only."""
    source = _load(source_id)
    chosen = _select_sha(source, sha)
    cache = _ensure_commit(source["url"], chosen)
    packages = _packages(_tree(cache, chosen))
    listed = []
    for root in sorted(packages)[:MAX_DISCOVERED]:
        package = packages[root]
        name = description = None
        skill_md = package["skill_md"]
        if skill_md is not None:
            text = _read_blob(cache, skill_md[2], skill_md[3]).decode("utf-8", "replace")
            meta = _frontmatter(text)
            name = meta.get("name") if isinstance(meta.get("name"), str) else None
            if isinstance(meta.get("description"), str):
                description = meta["description"][:MAX_DESCRIPTION]
        listed.append(
            {
                "path": package["path"],
                "name": name,
                "description": description,
                "files": len(package["files"]),
                "bytes": package["bytes"],
                "importable": not package["problems"],
                "problems": package["problems"][:20],
            }
        )
    return {
        "source": source,
        "sha": chosen,
        "packages": listed,
        "truncated": len(packages) > MAX_DISCOVERED,
    }


def refresh(source_id: Any, author: str | None = None) -> dict[str, Any]:
    """Re-resolve the source's ref and summarize the change. Never imports."""
    who = skills_registry._author(author)
    source = _load(source_id)
    old = source["pinned_sha"]
    _ensure_commit(source["url"], old)
    with _locked_cache(source["url"]) as cache:
        new = _fetch(source["url"], cache, source["ref"])
    changed = new != old
    diff = _diff_summary(cache, old, new) if changed else None
    now = storage.now()
    conn = storage.connect()
    publish_payload = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE skill_sources SET refreshed_sha=?, last_refreshed=? WHERE id=?",
            (new, now, source["id"]),
        )
        publish_payload = _record(
            conn,
            "skill.source_refreshed",
            {"source_id": source["id"], "pinned_sha": old, "resolved_sha": new},
            who,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM skill_sources WHERE id=?", (source["id"],)).fetchone()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    result = {
        "source": _public(row),
        "pinned_sha": old,
        "resolved_sha": new,
        "changed": changed,
        "diff": diff,
        "imported": False,
    }
    if changed:
        result["next"] = (
            "review the diff, then call source_import with sha=<resolved_sha> for each "
            "package to adopt; the pin moves only on that explicit import"
        )
    return result


def _diff_summary(cache: Path, old: str, new: str) -> dict[str, Any]:
    _, out = _git(
        cache,
        "diff-tree",
        "-r",
        "-z",
        "--no-renames",
        "--name-status",
        old,
        new,
        max_output=MAX_TREE_OUTPUT,
    )
    fields = [f for f in out.split(b"\0") if f]
    files = []
    total = 0
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        total += 1
        if len(files) < MAX_DIFF_FILES:
            files.append({"status": status.decode(), "path": path.decode("utf-8", "replace")})
    before = _packages(_tree(cache, old))
    after = _packages(_tree(cache, new))
    return {
        "files": files,
        "files_changed": total,
        "files_truncated": total > len(files),
        "packages_added": sorted(after[r]["path"] for r in set(after) - set(before)),
        "packages_removed": sorted(before[r]["path"] for r in set(before) - set(after)),
        "packages_changed": sorted(
            after[r]["path"]
            for r in set(after) & set(before)
            if after[r]["fingerprint"] != before[r]["fingerprint"]
        ),
    }


def _review(skill_name: str, members: dict[str, tuple[bytes, int]]) -> dict:
    """Run ``skill_review`` over a private, non-executable materialized copy."""
    scratch = config.data_home() / "skill_sources" / "review"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        root = Path(tmp) / skill_name
        for rel, (data, _mode) in members.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        review = review_skill_dir(root)
    review.pop("root", None)
    return review


def import_package(
    source_id: Any,
    path: str,
    name: str | None = None,
    sha: Any = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Import one package at an explicit SHA (pinned or last refreshed) as a version."""
    who = skills_registry._author(author)
    location = _location(path)
    if sha is None or (isinstance(sha, str) and not sha.strip()):
        raise ToolError(
            "source_import needs sha: the exact 40-character commit SHA you discovered "
            "or refreshed, so a pin that moved since then is refused rather than imported"
        )
    source = _load(source_id)
    chosen = _select_sha(source, sha)
    cache = _ensure_commit(source["url"], chosen)
    packages = _packages(_tree(cache, chosen))
    package = packages.get(location)
    if package is None:
        raise ToolError(
            f"no SKILL.md package at {location or '.'!r} in {chosen[:12]}; run source_discover"
        )
    if package["problems"]:
        raise ToolError("skill package refused: " + "; ".join(package["problems"][:5]))
    members: dict[str, tuple[bytes, int]] = {}
    for rel, mode, oid, size in package["files"]:
        data = _read_blob(cache, oid, size)
        members[rel] = (data, 0o755 if mode == "100755" and rel != "SKILL.md" else 0o644)
    try:
        text = members["SKILL.md"][0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("SKILL.md must be valid UTF-8") from exc
    fm_name = skills_registry._frontmatter_name(text)
    default_name = fm_name or PurePosixPath(location).name
    skill_name = skills_registry._validate_name(name or default_name)
    if fm_name is not None and fm_name != skill_name:
        raise ToolError(
            f"import name {skill_name!r} does not match the SKILL.md frontmatter name "
            f"{fm_name!r}; import under {fm_name!r} or fix the frontmatter upstream"
        )
    skills_registry._check_package_references(text, members)
    support_findings = skills_registry._audit_package(members)
    review = skills_registry._merge_package_findings(_review(skill_name, members), support_findings)
    provenance = {
        "source_id": source["id"],
        "url": source["url"],
        "sha": chosen,
        "path": location or ".",
    }
    if review["status"] == "fail":
        return {
            "ok": False,
            "blocked": True,
            "name": skill_name,
            "review": review,
            "provenance": provenance,
        }
    origin = f"git:{source['url']}@{chosen}:{location or '.'}"
    with skills_registry._MUTATION_LOCK:
        result = skills_registry._store_version(
            skill_name, members, text, origin, who, review, event_extra={"git": provenance}
        )
    result["provenance"] = provenance
    if chosen != source["pinned_sha"]:
        result["source"] = _advance_pin(source, chosen, who)
    return result


def _advance_pin(source: dict[str, Any], sha: str, who: str) -> dict[str, Any]:
    """Move the pin to ``sha`` after an explicit import at that SHA."""
    conn = storage.connect()
    publish_payload = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE skill_sources SET pinned_sha=? WHERE id=? AND pinned_sha=?",
            (sha, source["id"], source["pinned_sha"]),
        )
        if cur.rowcount:
            publish_payload = _record(
                conn,
                "skill.source_pinned",
                {"source_id": source["id"], "from_sha": source["pinned_sha"], "to_sha": sha},
                who,
            )
        conn.commit()
        row = conn.execute("SELECT * FROM skill_sources WHERE id=?", (source["id"],)).fetchone()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    return _public(row)
