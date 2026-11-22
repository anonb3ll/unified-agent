"""Audit engine: walks a project tree and applies the rules, read-only."""

import bisect
import fnmatch
import hashlib
import itertools
import json
import math
import os
import re
import stat
import subprocess
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .. import config
from ..telemetry.jwk import audit_span as jwk_audit_span
from ..telemetry.jwk import is_private_member, private_members
from . import baseline as audit_baseline
from .grade import grade_from_summary
from .rules import (
    _SHARED_SECRET_SHAPES,
    AUDIT_GIT_CALLER_ENV,
    AUDIT_GIT_ENV,
    CONTENT_RULES,
    JWK_MEMBER_MESSAGE_PREFIX,
    MCP_CONFIG_PATHS,
    REPO_RULES,
    SECRET_PATTERNS,
    SEVERITIES,
    SIGNATURE_SUFFIX,
    STAGED_CONFIG_PATHS,
    STAGED_REPO_RULES,
    SUPPRESS_MARKER,
    TRANSCRIPT_SUFFIX,
    VENDOR_CREDENTIAL_STORE_RX,
    Finding,
    _git_file_tracked,
    _git_has_network_remote,
    _is_exact_placeholder,
    audit_git_caller_env,
    audit_git_env,
    check_credential_store_mode,
    check_mcp_config,
    check_secrets,
    detached_signature,
    git_ignored_paths,
    signature_secret_finding,
    transcript_secret_finding,
)
from .safe_read import read_regular_file, read_regular_text

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    "target",
    # Task worktrees are full checkouts of other branches: scanning them from
    # the repo root reported every finding once per worktree and filled the
    # MAX_FILES cap with copies (#1386). Each worktree is audited on its own
    # by the pre-commit hook, where it is the root rather than a subdirectory.
    ".worktrees",
}

# Vendored third-party code that lives at a *nested* path, so a bare directory
# name in SKIP_DIRS cannot express it. Matched as a (parent, child) pair against
# the tail of the walk, which keeps the skip working wherever the walk starts.
#
# `.obsidian/plugins/` holds compiled community-plugin bundles — code we neither
# wrote nor can fix, the same footing as `node_modules`. One of them (obsidian-git)
# contains an `eval`, and as the vault's only `high` finding it alone made every
# vault audit report `status: fail`, draining the status field of information.
# Only `plugins` is skipped, not all of `.obsidian`: the JSON alongside it
# (app.json, community-plugins.json) is config we do control, and is exactly
# where a plugin-enablement change would show up.
SKIP_NESTED_DIRS = {
    (".obsidian", "plugins"),
    # Claude Code's own worktrees (EnterWorktree): full checkouts, like
    # `.worktrees/` above. Only this child is skipped; the rest of `.claude/`
    # (settings, hooks, skills) is config we own and stays in scope (#1386).
    (".claude", "worktrees"),
}

# Codex's own scratch and cache root, $CODEX_HOME/.tmp (#2316): the curated
# plugin repo clone (`plugins/`, `plugins.sha`, `plugins.sync.lock`) and the
# marketplace caches (`marketplaces/`, `bundled-marketplaces/`), all fetched by
# Codex. Walking it filled MAX_FILES and reported cached plugin code as ours.
# Bound to the configured Codex home itself, not to a `.codex/.tmp` name pair,
# and only outside every git work tree: a `.codex/.tmp` in a repository is
# source (PR #1378 review), and staged-blob inspection never skips it. The rest
# of the home (auth.json, config.toml, sessions, memories) stays in scope, so
# the store and permission checks still run.
CODEX_CACHE_DIR = ".tmp"


def codex_home() -> Path:
    """``UA_NODE_CODEX_HOME``, else ``$CODEX_HOME``, else ``~/.codex``.

    The same resolution as ``native_sessions.codex_home``, restated here
    because the audit engine (services layer) may not import the runtime
    layer; keep the two in step.
    """
    raw = os.environ.get("UA_NODE_CODEX_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(raw).expanduser()


def _codex_home_cache() -> str | None:
    """The real path of the Codex home's cache dir, or None when that home
    is missing or lies inside a git work tree (its files may be source)."""
    try:
        home = Path(os.path.realpath(codex_home()))
    except (OSError, ValueError, RuntimeError):
        return None
    if not home.is_dir():
        return None
    if any(_is_git_marker(d / ".git") for d in (home, *home.parents)):
        return None
    return str(home / CODEX_CACHE_DIR)


def _is_codex_home_cache(dirpath: str, name: str, cache: str | None) -> bool:
    if cache is None or name != CODEX_CACHE_DIR:
        return False
    try:
        return os.path.realpath(os.path.join(dirpath, name)) == cache
    except (OSError, ValueError):
        return False


MAX_FILES = 5000
MAX_FINDINGS = 500

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}  # high=0 … info=3


def _iter_files(
    root: Path, excludes: list[str] | None = None, repos: set[Path] | None = None
) -> tuple[list[Path], bool]:
    """Walk `root`. With `repos`, also collect every directory holding a
    `.git` work-tree marker, so a nested repository is found even when its
    only content is staged and deleted from its working copy (#2225)."""
    files: list[Path] = []
    eligible = 0  # names counted toward MAX_FILES; see _counts_toward_cap
    truncated = False
    excludes = excludes or []
    codex_cache = _codex_home_cache()
    for dirpath, dirnames, filenames in os.walk(root):
        parent = os.path.basename(dirpath)
        rel_dir = os.path.relpath(dirpath, root)
        if (
            repos is not None
            and (".git" in dirnames or ".git" in filenames)
            and _is_git_marker(Path(dirpath) / ".git")
        ):
            repos.add(Path(dirpath))
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or (parent, d) in SKIP_NESTED_DIRS:
                continue
            if _is_codex_home_cache(dirpath, d, codex_cache):
                continue
            # Prune whole subtrees that .ua-audit-ignore / --exclude already
            # covers, BEFORE the MAX_FILES cap is reached. Without this a root
            # with 100k+ vendored files (e.g. ~/.hermes with a cloned repo and a
            # fetch cache) fills the walk with excluded paths and truncates the
            # scan of the paths that matter. Test a hypothetical file *inside*
            # the dir, not the dir name itself: a file-oriented glob like
            # "test*" must exclude a root file test_x.py without pruning tests/.
            child_rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if excludes and _is_excluded(f"{child_rel}/__ua_probe__", excludes):
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if _counts_toward_cap(path):
                if eligible >= MAX_FILES:
                    truncated = True
                    return files, truncated
                eligible += 1
            files.append(path)
    return files, truncated


def _counts_toward_cap(path: Path) -> bool:
    """Whether `path` uses up one of the MAX_FILES slots (#2212).

    `os.walk` lists FIFOs, sockets and devices among the file names, and a
    `.git` FIFO is a name there too, so SKIP_DIRS never applies to it. Every
    name stays in the list, because the repo rules match on names (a `.env`
    link to /dev/null is still an exposed `.env`), but only a regular file,
    or a link to one, counts toward the cap: a tree of FIFOs cannot crowd out
    the files that are read. `_read_text` never opens the others. A name that
    cannot be stat'ed (a dangling link, a file removed mid-walk) counts, as
    it always has."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return True


def _read_text(path: Path) -> str | None:
    """Return decoded text, or None for binaries / oversized / unreadable files
    and anything that is not a regular file (see `read_regular_file`)."""
    data = read_regular_file(path, limit=config.SEARCH_FILE_MAX_BYTES)
    if data is None:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode(errors="replace")


def _is_git_marker(marker: Path) -> bool:
    """Whether `marker` (a `.git` path) makes its directory a work tree.

    A `.git` file (worktree or submodule) counts, and so does a `.git`
    directory with a HEAD; git ignores an empty `.git` directory, and so does
    this. A `.git` that exists but cannot be read (no permission, a dangling
    symlink, an error) counts too: the work tree is then real but its state
    unknown, which `_git_file_tracked` reports as None and callers keep at
    full severity. Reading it as "no git" would treat a tracked file as
    outside source control (PR #1299 review)."""
    try:
        os.lstat(marker)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not os.path.isdir(marker):
        # A regular file, a symlink to one, or a dangling symlink.
        return True
    if not os.access(marker, os.R_OK | os.X_OK):
        return True
    try:
        os.stat(os.path.join(marker, "HEAD"))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


class _GitLayout:
    """Which git work tree, if any, holds a directory. Found by walking up to
    a `.git` marker (see `_is_git_marker`), cached per audit run so a fresh
    `git init` is seen on the next run."""

    def __init__(self, bound: Path | None = None) -> None:
        self._cache: dict[Path, Path | None] = {}
        # A work tree GIT_DIR/GIT_WORK_TREE bind with no `.git` marker (#2225).
        self._bound = bound

    def work_tree(self, directory: Path) -> Path | None:
        seen = []
        d = directory
        while d not in self._cache:
            seen.append(d)
            marker = d / ".git"
            if d == self._bound or _is_git_marker(marker):
                found = d
                break
            if d.parent == d:
                found = None
                break
            d = d.parent
        else:
            found = self._cache[d]
        for s in seen:
            self._cache[s] = found
        return found


def _under_dot_dir(base: Path, relpath: str) -> bool:
    return base.name.startswith(".") or any(
        part.startswith(".") for part in relpath.split("/")[:-1]
    )


def _is_local_credential_store(path: Path, base: Path, relpath: str, git: _GitLayout) -> bool:
    """A file that holds a tool's own credentials rather than source (#1846):
    named like a vendor credential store, under a dot-directory, and either
    outside every git work tree or untracked. A tracked file is source.

    The vendor name is required even outside git: a stray copy of a key in a
    dot-directory (a backup, an exported env file) is exactly what the
    secrets rule exists to find, whatever its mode."""
    if not VENDOR_CREDENTIAL_STORE_RX.match(path.name):
        return False
    if not _under_dot_dir(base, relpath):
        return False
    if git.work_tree(path.parent) is None:
        return True
    # Unknown git state (None) is treated as tracked: the file stays source.
    return _git_file_tracked(path.parent, path.name) is False


# Recognized artifacts, not whole stores (#2157, #2221). Each glob matches
# exactly one path component; filenames and depth matter. Config alongside
# a transcript must keep full severity. See native_sessions.py for the Codex
# and Claude layouts. `.claude/sessions` is runtime state, not history.
AGENT_TRANSCRIPT_PATHS = (
    (".codex", "sessions", "[0-9][0-9][0-9][0-9]", "[0-9][0-9]", "[0-9][0-9]", "rollout-*.jsonl"),
    (".codex", "archived_sessions", "rollout-*.jsonl"),
    (".codex", "memories", "raw_memories.md"),
    (".codex", "memories", "MEMORY.md"),
    (".codex", "memories", "memory_summary.md"),
    (".codex", "memories", "rollout_summaries", "*.md"),
    (".claude", "projects", "*", "*.jsonl"),
    (".claude", "projects", "*", "*", "subagents", "agent-*.jsonl"),
    (".claude", "projects", "*", "memory", "*.md"),
    (".gemini", "tmp", "*", "chats", "session-*.json"),
    (".hermes", "sessions", "session_*.json"),
    (".hermes", "sessions", "*.jsonl"),
    (".hermes", "memories", "MEMORY.md"),
    (".hermes", "memories", "USER.md"),
)
_CLAUDE_SESSION_ID_RX = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_CODEX_NAME_RX = re.compile(
    r"rollout-(?P<stamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2})-"
    r"(?P<id>[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\.jsonl"
)
# Hermes 571a5911ae6: hermes_state_ids.py mint sites (not its prefix-only
# salvage recognizer); hermes_state.py writes <id>.jsonl, and
# hermes_state_sessions.py identifies legacy session_<id>.json snapshots.
_HERMES_SESSION_ID_RX = re.compile(
    r"(?:(?P<stamp>[0-9]{8}_[0-9]{6})_(?:[0-9a-f]{6}|[0-9a-f]{8}|[0-9a-f]{12})"
    r"|cron_[0-9a-f]{12}_(?P<cron_stamp>[0-9]{8}_[0-9]{6})"
    r"|bg_(?:(?P<time>[0-9]{6})_)?[0-9a-f]{6}|room_[0-9a-f]{32}"
    r"|api_[0-9]{9,}_[0-9a-f]{8}|api-[0-9a-f]{16}|run_[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
)
# Gemini CLI chatRecordingService: legacy JSON uses minute timestamp + the
# first eight UUID hex characters. 0.64.0 retains the legacy JSON reader;
# its newer JSONL/patch stream is intentionally outside this classifier.
_GEMINI_SESSION_NAME_RX = re.compile(
    r"session-(?P<stamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2})-[0-9a-f]{8}\.json"
)


def _minted_session_name(path: Path, harness: str) -> bool:
    if harness == ".gemini":
        match = _GEMINI_SESSION_NAME_RX.fullmatch(path.name)
        stamp_format = "%Y-%m-%dT%H-%M"
    else:
        sid = path.stem.removeprefix("session_") if path.suffix == ".json" else path.stem
        match = _HERMES_SESSION_ID_RX.fullmatch(sid)
        stamp_format = "%Y%m%d_%H%M%S"
    if match is None:
        return False
    groups = match.groupdict()
    stamp = groups.get("stamp") or groups.get("cron_stamp")
    try:
        if stamp:
            datetime.strptime(stamp, stamp_format)
        if groups.get("time"):
            datetime.strptime(groups["time"], "%H%M%S")
    except ValueError:
        return False
    return True


def _record_shape(value, required, optional=None) -> bool:
    """A supported JSON object, including the types of every declared member."""
    fields = required | (optional or {})
    return (
        isinstance(value, dict)
        and required.keys() <= value.keys() <= fields.keys()
        and all(
            (
                type(item) in (fields[key] if isinstance(fields[key], tuple) else (fields[key],))
                if isinstance(fields[key], (type, tuple))
                else fields[key](item)
            )
            for key, item in value.items()
        )
    )


def _identifier(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:@/+\-]+", value) is not None


def _uuid(value) -> bool:
    return isinstance(value, str) and _CLAUDE_SESSION_ID_RX.fullmatch(value) is not None


def _iso_time(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?",
        value,
    ):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _number(value) -> bool:
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _nullable(check):
    return lambda value: (
        value is None or (type(value) is check if isinstance(check, type) else check(value))
    )


def _tokens(value) -> bool:
    return _record_shape(
        value,
        dict.fromkeys(("input", "output", "cached", "total"), _number),
        dict.fromkeys(("thoughts", "tool"), _number),
    )


def _thoughts(value) -> bool:
    return isinstance(value, list) and all(
        _record_shape(item, {"subject": str, "description": str, "timestamp": _iso_time})
        for item in value
    )


def _scratchpad(value) -> bool:
    return _record_shape(
        value,
        {"version": lambda v: type(v) is int and v == 1},
        {
            "workflowSummary": str,
            "toolSequence": _strings,
            "touchedPaths": _strings,
            "validationStatus": lambda v: v in ("passed", "failed", "unknown"),
        },
    )


def _hermes_id(value) -> bool:
    return (
        isinstance(value, str)
        and _HERMES_SESSION_ID_RX.fullmatch(value) is not None
        and _minted_session_name(Path(value + ".jsonl"), ".hermes")
    )


def _hex_uid(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _display_metadata(value) -> bool:
    # Known scalar display contracts from Hermes response_filters, session_results,
    # warning_notifications and message_metadata. Unknown card schemas stay HIGH.
    return _record_shape(
        value,
        {},
        {
            "display_text": str,
            "title_preview": str,
            "error": str,
            "error_surface": _identifier,
            "gateway_input_owner": _nullable(_identifier),
            "reply_expected": bool,
            "notification_category": lambda v: v in ("diagnostic", "result"),
            "compaction_inflight_task_restatement": bool,
            "data_use": _strings,
        },
    )


def _reasoning_details(value) -> bool:
    if not isinstance(value, list):
        return False
    for detail in value:
        if not isinstance(detail, dict):
            return False
        if not isinstance(detail.get("type"), str):
            return False
        text_key = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(detail["type"])
        if text_key is None or not _record_shape(
            detail,
            {"type": str, text_key: str},
            {
                "signature": _nullable(str),
                "id": _nullable(_identifier),
                "format": _nullable(_identifier),
                "index": _nullable(_number),
            },
        ):
            return False
    return True


def _hermes_headers(message) -> bool:
    # _db_flush_row + copy_identity_fields at Hermes 571a5911ae6, plus core
    # legacy OpenAI history fields. Provider-specific replay objects are not opaque exemptions.
    optional = {
        "content": lambda v: True,
        "tool_calls": lambda v: True,
        "timestamp": _nullable(_number),
        "token_count": _nullable(_number),
        "_compressed_summary": bool,
        "observed": bool,
        "_row_id": lambda v: type(v) is int and v > 0,
        "message_uid": _hex_uid,
        "_tool_call_uid": _hex_uid,
        "_absorbed_message_uids": lambda v: isinstance(v, list) and all(_hex_uid(x) for x in v),
        "_tool_call_uids": lambda v: (
            isinstance(v, dict) and all(_identifier(k) and _hex_uid(x) for k, x in v.items())
        ),
        "display_metadata": _nullable(_display_metadata),
        "reasoning_details": _nullable(_reasoning_details),
        "codex_reasoning_items": type(None),
        "codex_message_items": type(None),
    }
    optional.update(
        dict.fromkeys(
            (
                "tool_name",
                "tool_call_id",
                "effect_disposition",
                "finish_reason",
                "platform_message_id",
                "message_id",
                "name",
                "display_kind",
            ),
            _nullable(_identifier),
        )
    )
    optional.update(
        dict.fromkeys(("reasoning", "reasoning_content", "api_content"), _nullable(str))
    )
    return _record_shape(message, {"role": str}, optional)


def _base_url(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme in {"http", "https"} and bool(url.netloc)
    except ValueError:
        return False


def _session_envelope(session, harness: str) -> bool:
    if harness == ".gemini":
        return _record_shape(
            session,
            {
                "sessionId": _uuid,
                "projectHash": lambda v: (
                    isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None
                ),
                "startTime": _iso_time,
                "lastUpdated": _iso_time,
                "messages": list,
            },
            {
                "summary": str,
                "memoryScratchpad": _scratchpad,
                "directories": _strings,
                "kind": lambda v: v in ("main", "subagent"),
            },
        )
    # Original Hermes _save_session_log, removed in 7a5fc1b2a9e; the
    # legacy snapshot's metadata is a closed schema, not an arbitrary mapping.
    return _record_shape(
        session,
        {
            "session_id": _hermes_id,
            "model": _identifier,
            "base_url": _base_url,
            "platform": _nullable(_identifier),
            "session_start": _iso_time,
            "last_updated": _iso_time,
            "system_prompt": str,
            "tools": lambda v: isinstance(v, list) and all(_identifier(x) for x in v),
            "message_count": lambda v: type(v) is int and v >= 0,
            "messages": list,
        },
    ) and session["message_count"] == len(session["messages"])


def _gemini_part(value) -> bool:
    # PartListUnion / partUtils: strings or flat arrays of strings/Parts.
    # A Part has exactly one payload; thought/signature are metadata.
    if isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    payload = {k: v for k, v in value.items() if k not in {"thought", "thoughtSignature"}}
    if len(payload) != 1:
        return False
    if "thought" in value and type(value["thought"]) is not bool:
        return False
    if "thoughtSignature" in value and not isinstance(value["thoughtSignature"], str):
        return False
    key, part = next(iter(payload.items()))
    if key == "text":
        return isinstance(part, str)
    schemas = {
        "inlineData": ({"mimeType": str, "data": str}, {}),
        "fileData": ({"fileUri": str}, {"mimeType": str}),
        "functionCall": ({"name": str}, {"args": dict, "id": str}),
        "functionResponse": ({"name": str, "response": dict}, {"id": str}),
        "executableCode": ({"language": str, "code": str}, {}),
        "codeExecutionResult": ({"outcome": str}, {"output": str}),
    }
    if key not in schemas or not _record_shape(part, *schemas[key]):
        return False
    if key == "executableCode":
        return part["language"] in {"LANGUAGE_UNSPECIFIED", "PYTHON"}
    if key == "codeExecutionResult":
        return part["outcome"] in {
            "OUTCOME_UNSPECIFIED",
            "OUTCOME_OK",
            "OUTCOME_FAILED",
            "OUTCOME_DEADLINE_EXCEEDED",
        }
    return key not in {"functionCall", "functionResponse"} or bool(part["name"])


def _gemini_content(value) -> bool:
    if isinstance(value, list):
        return bool(value) and all(_gemini_part(part) for part in value)
    return _gemini_part(value)


def _hermes_block(value) -> bool:
    # Hermes message_content / vision_message_prep / anthropic_message_convert.
    if isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    block = dict(value)
    if "cache_control" in block and block.pop("cache_control") != {"type": "ephemeral"}:
        return False
    kind = block.get("type")
    if kind in ("text", "input_text"):
        return _record_shape(block, {"type": str, "text": str})
    if kind in ("image_url", "input_image"):
        if not _record_shape(block, {"type": str, "image_url": (str, dict)}):
            return False
        image = block["image_url"]
        return (
            bool(image)
            if isinstance(image, str)
            else (
                _record_shape(image, {"url": str}, {"detail": str})
                and bool(image["url"])
                and image.get("detail", "auto") in {"auto", "low", "high"}
            )
        )
    if kind == "image" and _record_shape(block, {"type": str, "source": dict}):
        source = block["source"]
        return (
            source.get("type") == "url" and _record_shape(source, {"type": str, "url": str})
        ) or (
            source.get("type") == "base64"
            and _record_shape(source, {"type": str, "media_type": str, "data": str})
        )
    return False


def _hermes_calls(calls) -> bool:
    return isinstance(calls, list) and all(
        _record_shape(call, {"id": str, "type": str, "function": dict})
        and bool(call["id"])
        and call["type"] == "function"
        and _record_shape(call["function"], {"name": str, "arguments": str})
        and bool(call["function"]["name"])
        for call in calls
    )


def _session_message(message, harness: str) -> bool:
    if not isinstance(message, dict):
        return False
    if harness == ".gemini":
        optional = {"displayContent": _gemini_content}
        if message.get("type") == "gemini":
            optional.update(
                {
                    "toolCalls": list,
                    "thoughts": _thoughts,
                    "tokens": _nullable(_tokens),
                    "model": _identifier,
                }
            )
        if not _record_shape(
            message,
            {
                "id": _uuid,
                "timestamp": _iso_time,
                "type": str,
                "content": lambda v: True,
            },
            optional,
        ):
            return False
        if message.get("type") not in ("user", "gemini", "info", "error", "warning"):
            return False
        if "toolCalls" in message:
            calls = message["toolCalls"]
            if message["type"] != "gemini" or not isinstance(calls, list):
                return False
            for call in calls:
                if not _record_shape(
                    call,
                    {
                        "id": _identifier,
                        "name": _identifier,
                        "args": dict,
                        "status": str,
                        "timestamp": _iso_time,
                    },
                    {
                        "result": (str, dict, list, type(None)),
                        "displayName": str,
                        "description": str,
                        "resultDisplay": str,
                        "renderOutputAsMarkdown": bool,
                        "agentId": _identifier,
                    },
                ):
                    return False
                if not call["id"] or not call["name"]:
                    return False
                if call["status"] not in {
                    "validating",
                    "scheduled",
                    "error",
                    "success",
                    "executing",
                    "cancelled",
                    "awaiting_approval",
                }:
                    return False
                if call.get("result") is not None and not _gemini_content(call["result"]):
                    return False
        return _gemini_content(message.get("content"))
    if not _hermes_headers(message):
        return False
    role = message.get("role")
    if role not in ("user", "assistant", "system", "developer", "tool", "function"):
        return False
    calls = message.get("tool_calls")
    if calls is not None and (not _hermes_calls(calls) or (calls and role != "assistant")):
        return False
    content = message.get("content")
    if content is None or content == []:
        return role == "assistant" and bool(calls)
    return isinstance(content, str) or (
        isinstance(content, list) and all(_hermes_block(part) for part in content)
    )


def _integer(value) -> bool:
    return type(value) is int and value >= 0


def _empty_list(value) -> bool:
    return isinstance(value, list) and not value


def _native_path(value) -> bool:
    return (
        isinstance(value, str)
        and "\x00" not in value
        and (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) is not None)
    )


def _git_branch(value) -> bool:
    # Git check-ref-format --branch, without checkout-syntax expansion or I/O.
    # Codex's collector omits detached HEAD rather than recording that sentinel.
    return (
        isinstance(value, str)
        and bool(value)
        and value != "HEAD"
        and not value.startswith("-")
        and not value.endswith(".")
        and ".." not in value
        and "@{" not in value
        and not any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in value)
        and all(p and not p.startswith(".") and not p.endswith(".lock") for p in value.split("/"))
    )


# Exact paths in already-validated artifacts, not key-name exemptions. Only
# declared history/payload slots can contain secrets without disqualifying the
# artifact. Asterisks stand for array elements; opaque tool inputs are not walked.
_HISTORY_PATHS = {
    ".codex": (
        "*.payload.base_instructions.text",
        "*.payload.content.*.text",
        "*.payload.arguments",
        "*.payload.output",
        "*.payload.output.*.text",
        "*.payload.message",
        "*.payload.images.*",
        "*.payload.encrypted_content",
        "*.payload.encrypted_function_args.*",
    ),
    ".claude": (
        "*.message.content",
        "*.message.content.*.text",
        "*.message.content.*.thinking",
        "*.message.content.*.data",
        "*.message.content.*.source.data",
        "*.message.content.*.source.url",
        "*.message.content.*.input",
        "*.message.content.*.content",
        "*.message.content.*.content.*.text",
        "*.message.content.*.content.*.source.data",
        "*.message.content.*.content.*.source.url",
    ),
    ".gemini": (
        "summary",
        "memoryScratchpad.workflowSummary",
        "messages.*.thoughts.*.subject",
        "messages.*.thoughts.*.description",
        "messages.*.toolCalls.*.args",
        "messages.*.toolCalls.*.resultDisplay",
        *(
            prefix + suffix
            for prefix in (
                "messages.*.content",
                "messages.*.content.*",
                "messages.*.displayContent",
                "messages.*.displayContent.*",
                "messages.*.toolCalls.*.result",
                "messages.*.toolCalls.*.result.*",
            )
            for suffix in (
                "",
                ".text",
                ".inlineData.data",
                ".fileData.fileUri",
                ".functionCall.args",
                ".functionResponse.response",
                ".executableCode.code",
                ".codeExecutionResult.output",
            )
        ),
    ),
    ".hermes": (
        "system_prompt",
        "messages.*.content",
        "messages.*.content.*",
        "messages.*.content.*.text",
        "messages.*.content.*.image_url",
        "messages.*.content.*.image_url.url",
        "messages.*.content.*.source.url",
        "messages.*.content.*.source.data",
        "messages.*.tool_calls.*.function.arguments",
        "messages.*.reasoning",
        "messages.*.reasoning_content",
        "messages.*.api_content",
        "messages.*.reasoning_details.*.text",
        "messages.*.reasoning_details.*.summary",
        "messages.*.display_metadata.display_text",
        "messages.*.display_metadata.title_preview",
        "messages.*.display_metadata.error",
    ),
}
_HISTORY_PATHS = {h: frozenset(tuple(p.split(".")) for p in ps) for h, ps in _HISTORY_PATHS.items()}
_STRUCTURED_HISTORY_PATHS = {
    ".codex": frozenset(),
    ".claude": frozenset({("*", "message", "content", "*", "input")}),
    ".gemini": frozenset(p for p in _HISTORY_PATHS[".gemini"] if p[-1] in {"args", "response"}),
    ".hermes": frozenset(),
}


def _transcript_metadata_clean(artifact, harness: str) -> bool:
    """Reuse the secret rule on metadata once, without auditing or reading again.

    Valid Git refs can contain Python kwargs holding a key, so format checks
    alone cannot protect metadata. Walk iteratively; skip arbitrary tool payloads
    before visiting them. All other containers have already passed closed schemas.
    Frame each scalar as a JSON string: an unfinished fragment must not change
    the lexical ownership of another scalar, array element or record. This uses
    the same literal encoding level as strings in the original JSON artifact.
    """
    strings = []
    pending = [(artifact, ())]
    while pending:
        value, path = pending.pop()
        if path in _STRUCTURED_HISTORY_PATHS[harness]:
            continue
        if isinstance(value, str):
            if path not in _HISTORY_PATHS[harness]:
                strings.append(value)
        elif isinstance(value, dict):
            strings.extend(value)  # Dynamic identity-map keys are metadata too.
            pending.extend((v, path + (k,)) for k, v in value.items())
        elif isinstance(value, list):
            pending.extend((v, path + ("*",)) for v in value)
    return next(check_secrets("transcript-metadata", json.dumps(strings)), None) is None


def _codex_name(path: Path):
    match = _CODEX_NAME_RX.fullmatch(path.name)
    if match is None:
        return None
    try:
        datetime.strptime(match["stamp"], "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None
    # The writer uses the same local calendar for basename and dated directory.
    if (
        path.parent.name != "archived_sessions"
        and tuple(match["stamp"][:10].split("-")) != path.parts[-4:-1]
    ):
        return None
    return match


def _codex_meta(value) -> bool:
    # Codex rust-v0.164.0 (6aaac6948030), SessionMetaLine. Only closed,
    # scalar/text metadata extensions are supported; opaque future structures stay HIGH.
    return _record_shape(
        value,
        {
            "id": _uuid,
            "timestamp": _iso_time,
            "cwd": _native_path,
            "originator": _identifier,
            "cli_version": _identifier,
            "source": lambda v: v in ("cli", "vscode", "exec", "mcp", "unknown"),
        },
        {
            "session_id": _uuid,
            "model_provider": _nullable(_identifier),
            "base_instructions": _nullable(lambda v: _record_shape(v, {"text": str})),
            "git": _nullable(
                lambda v: _record_shape(
                    v,
                    {},
                    {
                        "commit_hash": lambda x: (
                            isinstance(x, str)
                            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x) is not None
                        ),
                        "branch": _git_branch,
                        "repository_url": str,
                    },
                )
            ),
            "creator_user_id": _nullable(_identifier),
            "creator_account_id": _nullable(_identifier),
            "forked_from_id": _nullable(_uuid),
            "parent_thread_id": _nullable(_uuid),
            "forked_from_ordinal_exclusive": _nullable(_integer),
            "runtime_workspace_roots": _nullable(
                lambda v: isinstance(v, list) and all(_native_path(x) for x in v)
            ),
            "thread_source": _nullable(_identifier),
            "agent_nickname": _nullable(str),
            "agent_role": _nullable(_identifier),
            "agent_type": _nullable(_identifier),
            "agent_path": _nullable(str),
            "memory_mode": _nullable(_identifier),
            "history_mode": lambda v: v in ("legacy", "paginated"),
            "subagent_history_start_ordinal": _nullable(_integer),
            "dynamic_tools": _nullable(_empty_list),
            "selected_capability_roots": _empty_list,
            "history_base": type(None),
            "multi_agent_version": type(None),
            "context_window": type(None),
        },
    )


def _codex_content(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(part, dict)
        and part.get("type") in ("input_text", "output_text")
        and _record_shape(
            part,
            {"type": str, "text": str},
            {
                "annotations": _nullable(_empty_list),
                "logprobs": _nullable(_empty_list),
            }
            if part["type"] == "output_text"
            else {},
        )
        for part in value
    )


def _codex_response(value) -> bool:
    if not isinstance(value, dict):
        return False
    common = {
        "id": _nullable(_identifier),
        "encrypted_content": _nullable(str),
        "status": _nullable(_identifier),
        "internal_chat_message_metadata_passthrough": type(None),
    }
    if value.get("type") == "message":
        return _record_shape(
            value,
            {
                "type": str,
                "role": lambda v: v in ("user", "assistant", "system", "developer"),
                "content": lambda v: bool(v) and _codex_content(v),
            },
            common | {"phase": _nullable(lambda v: v in ("commentary", "final_answer"))},
        )
    if value.get("type") == "function_call":
        return _record_shape(
            value,
            {"type": str, "name": _identifier, "call_id": _identifier, "arguments": str},
            common
            | {"namespace": _nullable(_identifier), "encrypted_function_args": _nullable(_strings)},
        )
    if value.get("type") == "function_call_output":
        return _record_shape(
            value,
            {"type": str, "output": lambda v: isinstance(v, str) or _codex_content(v)},
            {
                "id": _nullable(_identifier),
                "call_id": _nullable(_identifier),
                "name": _nullable(_identifier),
                "namespace": _nullable(_identifier),
                "internal_chat_message_metadata_passthrough": type(None),
            },
        )
    return False


def _codex_event(value) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "user_message":
        return _record_shape(
            value,
            {"type": str, "message": str},
            {
                "images": _nullable(_strings),
                "local_images": _strings,
                "file_ids": _nullable(_strings),
                "text_elements": _empty_list,
                "client_id": _nullable(_identifier),
            },
        )
    if value.get("type") == "agent_message":
        return _record_shape(
            value,
            {"type": str, "message": str},
            {
                "phase": _nullable(lambda v: v in ("commentary", "final_answer")),
                "memory_citation": type(None),
                "delivery": type(None),
                "questions": _nullable(_empty_list),
            },
        )
    if value.get("type") == "token_count":
        counters = dict.fromkeys(
            (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ),
            _integer,
        )

        def usage(v):
            return _record_shape(v, counters, {"cache_write_input_tokens": _integer})

        return _record_shape(
            value,
            {
                "type": str,
                "info": _nullable(
                    lambda v: _record_shape(
                        v,
                        {
                            "total_token_usage": usage,
                            "last_token_usage": usage,
                            "model_context_window": _nullable(_integer),
                        },
                    )
                ),
            },
            {"rate_limits": type(None)},
        )
    return False


def _codex_record(record, first: bool) -> bool:
    if not isinstance(record, dict):
        return False
    kind = record.get("type")
    if first:
        check = _codex_meta if kind == "session_meta" else None
    else:
        check = (
            _codex_response
            if kind == "response_item"
            else _codex_event
            if kind == "event_msg"
            else None
        )
    return check is not None and _record_shape(
        record,
        {
            "timestamp": _iso_time,
            "type": str,
            "payload": check,
        },
        {"ordinal": _integer, **({"metadata": type(None)} if kind == "response_item" else {})},
    )


def _claude_block(part, role: str) -> bool:
    if not isinstance(part, dict):
        return False
    kind = part.get("type")
    cache = {"cache_control": _nullable(lambda v: v == {"type": "ephemeral"})}
    if kind == "text":
        return _record_shape(
            part, {"type": str, "text": str}, cache | {"citations": _nullable(_empty_list)}
        )
    if kind == "image":
        return role == "user" and _hermes_block(part)
    if kind == "tool_use":
        return role == "assistant" and _record_shape(
            part,
            {
                "type": str,
                "id": _identifier,
                "name": _identifier,
                "input": lambda v: True,
            },
            cache
            | {"caller": lambda v: v == {"type": "direct"}, "toolset_name": _nullable(_identifier)},
        )
    if kind == "tool_result":
        return role == "user" and _record_shape(
            part,
            {"type": str, "tool_use_id": _identifier},
            cache
            | {
                "is_error": bool,
                "toolset_name": _nullable(_identifier),
                "content": lambda v: (
                    isinstance(v, str)
                    or (
                        isinstance(v, list)
                        and all(
                            isinstance(x, dict)
                            and x.get("type") in ("text", "image")
                            and _claude_block(x, "user")
                            for x in v
                        )
                    )
                ),
            },
        )
    if kind == "thinking":
        return role == "assistant" and _record_shape(
            part, {"type": str, "thinking": str, "signature": str}
        )
    if kind == "redacted_thinking":
        return role == "assistant" and _record_shape(part, {"type": str, "data": str})
    return False


def _claude_usage(value) -> bool:
    return _record_shape(
        value,
        {"input_tokens": _number, "output_tokens": _number},
        {
            "cache_creation_input_tokens": _nullable(_number),
            "cache_read_input_tokens": _nullable(_number),
            "cache_creation": _nullable(
                lambda v: _record_shape(
                    v, {"ephemeral_5m_input_tokens": _number, "ephemeral_1h_input_tokens": _number}
                )
            ),
            "server_tool_use": _nullable(
                lambda v: _record_shape(
                    v, {}, {"web_search_requests": _number, "web_fetch_requests": _number}
                )
            ),
            "service_tier": _nullable(lambda v: v in ("standard", "priority", "batch")),
            "inference_geo": _nullable(_identifier),
            "output_tokens_details": type(None),
        },
    )


def _claude_record(record, session_id: str) -> bool:
    if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
        return False
    role = record["type"]

    def message(value):
        if role == "user":
            return _record_shape(
                value,
                {
                    "role": lambda v: v == role,
                    "content": lambda v: (
                        isinstance(v, str)
                        or (
                            isinstance(v, list)
                            and bool(v)
                            and all(_claude_block(x, role) for x in v)
                        )
                    ),
                },
            )
        return _record_shape(
            value,
            {
                "role": lambda v: v == role,
                "type": lambda v: v == "message",
                "id": _identifier,
                "model": _identifier,
                "content": lambda v: (
                    isinstance(v, list) and bool(v) and all(_claude_block(x, role) for x in v)
                ),
                "usage": _claude_usage,
                "stop_reason": _nullable(
                    lambda v: (
                        v
                        in (
                            "end_turn",
                            "max_tokens",
                            "stop_sequence",
                            "tool_use",
                            "pause_turn",
                            "refusal",
                            "model_context_window_exceeded",
                        )
                    )
                ),
                "stop_sequence": _nullable(str),
            },
            {"container": type(None), "stop_details": type(None)},
        )

    return _record_shape(
        record,
        {
            "type": str,
            "message": message,
            "sessionId": lambda v: _uuid(v) and v == session_id,
            "uuid": _uuid,
            "parentUuid": _nullable(_uuid),
            "timestamp": _iso_time,
            "cwd": _native_path,
            "version": _identifier,
            "isSidechain": bool,
            "userType": lambda v: v in ("external", "internal"),
        },
        {
            "gitBranch": _git_branch,
            "slug": str,
            "agentId": _identifier,
            "isMeta": bool,
            "isCompactSummary": bool,
            "sourceToolAssistantUUID": _nullable(_uuid),
            "requestId": _nullable(_identifier),
            "teamName": _nullable(str),
            "agentName": _nullable(str),
            "sessionKind": _identifier,
            "forkedFrom": lambda v: _record_shape(v, {"sessionId": _uuid, "messageUuid": _uuid}),
        },
    )


def _native_jsonl(path: Path, text: str, harness: str, session_id=None) -> bool:
    name = _codex_name(path) if harness == ".codex" else None
    if harness == ".codex" and name is None:
        return False
    try:
        records = [_session_json_loads(line) for line in text.splitlines() if line.strip()]
        if not records:
            return False
        if harness == ".codex":
            return (
                all(_codex_record(r, i == 0) for i, r in enumerate(records))
                and records[0]["payload"]["id"] == name["id"]
                and _transcript_metadata_clean(records, harness)
            )
        return all(_claude_record(r, session_id) for r in records) and _transcript_metadata_clean(
            records, harness
        )
    except (ValueError, RecursionError):
        return False


def _reject_nonfinite(value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    result = float(value)
    return result if math.isfinite(result) else _reject_nonfinite(value)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _session_json_loads(text):
    return json.loads(
        text,
        parse_constant=_reject_nonfinite,
        parse_float=_finite_float,
        object_pairs_hook=_unique_object,
    )


def _has_session_messages(text: str, harness: str, *, jsonl: bool = False) -> bool:
    """Disambiguate JSON session logs from config; malformed/unknown is source.

    Use the already bounded audit text, only after a secrets finding. Hermes
    JSONL has no fixed filename prefix, so it needs message records too.
    """
    try:
        if jsonl:
            messages = [_session_json_loads(line) for line in text.splitlines() if line.strip()]
        else:
            session = _session_json_loads(text)
            if not _session_envelope(session, harness):
                return False
            messages = session["messages"]
    except (ValueError, RecursionError):
        return False
    if not isinstance(messages, list) or not messages:
        return False
    return all(
        _session_message(message, harness) for message in messages
    ) and _transcript_metadata_clean({"messages": messages} if jsonl else session, harness)


def _claude_session_id(path: Path, pattern: tuple) -> str:
    return path.stem if len(pattern) == 4 else path.parent.parent.name


def _agent_log_match(path: Path) -> tuple[Path, tuple] | None:
    """The store directory and pattern of a known agent-log artifact: its
    whole harness-relative path matches, wherever rooted, with the file name
    the harness mints where it mints one. The content is not read here."""
    parts = path.parts
    for i, part in enumerate(parts[:-1]):
        for pattern in AGENT_TRANSCRIPT_PATHS:
            if part != pattern[0] or len(parts) - i != len(pattern):
                continue
            if not all(
                fnmatch.fnmatchcase(got, want) for got, want in zip(parts[i:], pattern, strict=True)
            ):
                continue
            if part == ".claude" and path.suffix == ".jsonl":
                if not _CLAUDE_SESSION_ID_RX.fullmatch(_claude_session_id(path, pattern)):
                    return None
                if len(pattern) != 4 and not re.fullmatch(
                    r"agent-[A-Za-z0-9_-]+\.jsonl", path.name
                ):
                    return None
            elif part == ".codex" and path.suffix == ".jsonl":
                if _codex_name(path) is None:
                    return None
            elif part == ".gemini" or (part == ".hermes" and pattern[1] == "sessions"):
                if not _minted_session_name(path, part):
                    return None
            return Path(*parts[: i + 2]), pattern
    return None


def _native_artifact(path: Path, text: str, pattern: tuple) -> bool:
    """Whether a matched artifact also passes its harness's closed schema
    (#2221). Memory files have no schema: their path is the whole proof."""
    harness = pattern[0]
    if harness == ".claude" and path.suffix == ".jsonl":
        return _native_jsonl(path, text, harness, _claude_session_id(path, pattern))
    if harness == ".codex" and path.suffix == ".jsonl":
        return _native_jsonl(path, text, harness)
    if harness == ".gemini" or (harness == ".hermes" and pattern[1] == "sessions"):
        return _has_session_messages(text, harness, jsonl=path.suffix == ".jsonl")
    return True


def _in_transcript_store(path: Path, text: str) -> bool:
    """Match a known artifact's entire harness-relative path, wherever rooted,
    and its schema."""
    match = _agent_log_match(path)
    return match is not None and _native_artifact(path, text, match[1])


def _local_only_repo(tree: Path) -> bool:
    """Whether no commit in `tree` can leave the machine: every remote URL is
    a local path (`rules._git_has_network_remote`, both git views) or there is
    no remote at all in either view. Anything unknown is False."""
    network = _git_has_network_remote(tree)
    if network is not None:
        return network is False
    # None: no remote, or the remotes could not be listed. Only a positive
    # empty list counts, in the isolated view and in the caller's, whose
    # config can define a remote for every repository.
    views = [audit_git_env(tree)]
    if AUDIT_GIT_CALLER_ENV.get() is not None:
        views.append(audit_git_caller_env(tree))
    for env in views:
        try:
            res = _git_exec(tree, ("remote",), dict(os.environ if env is None else env), None)
        except _GitFailure:
            return False
        if res.returncode != 0 or res.stdout.strip():
            return False
    return True


def _signed_document(
    path: Path, base: Path, relpath: str, text: str, git: _GitLayout
) -> Path | None:
    """The document ``path`` is a detached signature of, or ``None`` (task #2419).

    Four things must hold: the file is in a dot-directory outside every git
    work tree, its name ends in ``SIGNATURE_SUFFIX``, the document it signs
    sits beside it, and the content is exactly one JWT
    (``rules.detached_signature``). A committer controls the name, the sibling
    and the shape, so location is the check that makes the other three mean
    anything. Outside every work tree nothing can be staged, so the staged-blob
    scan never needs this answer.
    """
    if not relpath.endswith(SIGNATURE_SUFFIX) or not _under_dot_dir(base, relpath):
        return None
    if git.work_tree(path.parent) is not None:
        return None
    name = path.name[: -len(SIGNATURE_SUFFIX)]
    if not name or name in (".", ".."):
        return None
    try:
        signed = path.parent / name
        if not signed.is_file():
            return None
    except OSError:
        return None
    return signed if detached_signature(text) else None


def _agent_log_class(
    path: Path, git: _GitLayout, text: str, untracked, local_only, enclosing_untracked
) -> str | None:
    """`"transcript"` for a known agent-log artifact that passes its schema,
    `"log"` for one only its path places in a store (#2296: a newer harness
    release writes records the closed schema does not know), None for source.

    Either way the file must be outside source control: outside every git
    work tree, positively untracked (#2157), or tracked only in the harness's
    own history repository, one rooted at the store directory itself (Codex
    keeps `memories/` as a git baseline) with no remote a commit could reach
    (#2296). A repository rooted above the store, a network remote, or an
    unknown git state keeps the file source, and so does any enclosing
    repository whose index holds the path (PR #1375 review: a nested
    local-only repo must not hide an outer one that tracks the same file).
    Called only for a file that has a SECRETS-KEY finding, so the git checks
    cost nothing elsewhere."""
    match = _agent_log_match(path)
    if match is None:
        return None
    store, pattern = match
    tree = git.work_tree(path.parent)
    if tree is not None and not (tree == store and local_only(tree)):
        # Only a positive "not in the index" relaxes the finding; an unknown
        # (corrupt index, git failure) keeps it at full severity.
        if not (_git_file_tracked(path.parent, path.name) is False and untracked(path)):
            return None
    if tree is not None and not enclosing_untracked(path, tree):
        return None
    return "transcript" if _native_artifact(path, text, pattern) else "log"


# SECRETS-KEY in the agent-log class (#2296). A harness logs every file and
# document an agent read, and writes each record as one JSONL line, so a
# probe there meets fixtures, test vectors and whole embedded documents. The
# finding is restated by `rules.transcript_secret_finding`, then graded:
#   - the JWK heuristic in a schema-verified transcript stays low (#2157,
#     unchanged: its history holds whatever test vectors an agent read);
#   - anything else is high when its line holds live-looking key material,
#     otherwise medium. Medium and low stay visible but do not fail
#     `--fail-on high`; nothing is dropped.
# Live-looking material (PR #1375 review) is judged on extracted values,
# never on match length:
#   - payload escapes are decoded first (`\n`, `\"`, `\uXXXX`, nested up to
#     _DECODE_ROUNDS deep), so a JSON-escaped line break or quote ends a
#     token where it ends in the document; every secret pattern then runs
#     over the decoded line, and any match counts, not only the first;
#   - a value is a stand-in only when the WHOLE value, or the whole part
#     after a vendor prefix, is a placeholder: `rules._is_exact_placeholder`
#     shapes (`<...>`, `${...}`, `****`, `[REDACTED]`), one repeated filler
#     character, nothing but stand-in words and filler
#     (`FAKE-FIXTURE-NOT-A-KEY-000`), a documented example key, or a URL
#     password equal to its user. A marker inside opaque text is not one;
#   - for a JWK finding, a private member reported by
#     `telemetry.jwk.private_members` counts when its name is a JWK private
#     parameter (d p q dp dq qi k, and oth's r and t) and its value, with
#     YAML tags, quotes, whitespace and PEM armor removed, is opaque base64
#     that is not a stand-in, or a colon-separated hex string of at least
#     16 bytes. A private key is not only one uninterrupted base64 token.
#     Other names are the scanner's fail-closed masking of record fields
#     (`id`, `turn_id`, `type` on real rollouts), not key material.
# Repo source, `.env` files, credential stores and file modes are untouched:
# only files `_agent_log_class` places in the class are graded so.
AGENT_LOG_SECRET_SEVERITY = "medium"
_DECODE_ROUNDS = 4
_ESCAPE_RX = re.compile(r"\\(?:u([0-9a-fA-F]{4})|(.))", re.DOTALL)
_ESCAPED_CHARS = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_VENDOR_PREFIX_RX = re.compile(
    r"^(?:sk-ant-(?:[a-z]+\d+-)?|sk-(?:proj-)?|github_pat_|gh[pousr]_|xox[baprs]-|xapp-"
    r"|AKIA|ASIA|AIza|[sr]k_(?:live|test)_|uas_)"
)
_STAND_IN_VALUE_RX = re.compile(
    r"(?:(?:fake|fixture|dummy|example|sample|test|placeholder|redacted|changeme|not|a|real"
    r"|key|secret|token|value|your|here|my|api|access|private)[-_. ]?)+[0x*.\u2022_-]*",
    re.IGNORECASE,
)
# Published documentation keys (AWS's own examples).
_DOCUMENTED_EXAMPLE_KEYS = frozenset(
    {"AKIAIOSFODNN7" + "EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"}
)
_URL_PASSWORD_USER_RX = re.compile(r"://([^\s/:@]+):")
_OPAQUE_KEY_RX = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_YAML_TAG_RX = re.compile(r"^!(?:<[^>\s]*>|[!\w.:/-]*)\s+")
_PEM_ARMOR_RX = re.compile(r"-----BEGIN [A-Z0-9 ]*-----|-----END [A-Z0-9 ]*-----")
_COLON_HEX_RX = re.compile(r"(?:[0-9a-fA-F]{2}:){15,}[0-9a-fA-F]{2}")
_SECRET_SHAPES = (*SECRET_PATTERNS, *_SHARED_SECRET_SHAPES)
ANY_KEY_TYPE = "*"  # telemetry.jwk: the union of every key type's private names
_OTH_PRIVATE = frozenset({"r", "t"})  # RFC 7518 6.3.2.7: oth's r, d and t


def _decoded(line: str) -> str:
    for _ in range(_DECODE_ROUNDS):
        out = _ESCAPE_RX.sub(
            lambda m: chr(int(m[1], 16)) if m[1] else _ESCAPED_CHARS.get(m[2], m[2]), line
        )
        if out == line:
            break
        line = out
    return line


def _whole_stand_in(value: str, matched: str = "") -> bool:
    v = value.strip().strip("\"'")
    if not v or _is_exact_placeholder(v) or v in _DOCUMENTED_EXAMPLE_KEYS:
        return True
    user = _URL_PASSWORD_USER_RX.search(matched)
    if user and user.group(1).lower() == v.lower():
        return True
    rest = _VENDOR_PREFIX_RX.sub("", v, count=1)
    return (
        not rest
        or len(set(rest.lower())) == 1
        or _is_exact_placeholder(rest)
        or bool(_STAND_IN_VALUE_RX.fullmatch(rest))
    )


def _line_tokens(line: str) -> list[tuple[str, str]]:
    """(pattern name, value) of every secret-pattern match on the decoded line
    that is not a whole-value stand-in, so neither an over-match nor a
    placeholder can shadow a real key beside it."""
    text = _decoded(line)
    tokens = []
    for name, rx in _SECRET_SHAPES:
        for m in rx.finditer(text):
            value = m.groupdict().get("val") or m.group(0)
            if not _whole_stand_in(value, m.group(0)):
                tokens.append((name, value))
    return tokens


def _line_holds_token(line: str) -> bool:
    return bool(_line_tokens(line))


def _jwk_value(raw: str) -> str:
    value = raw.strip()
    for _ in range(2):
        value = _YAML_TAG_RX.sub("", value, count=1).strip().strip("\"'").strip()
    return value


def _jwk_live_material(value: str) -> bool:
    """True when a JWK private member holds key material, not a stand-in.

    Whitespace and PEM armor are delimiters around the same opaque value.
    A colon-separated hex string of at least 16 bytes is key material too,
    including when the hex is wrapped across lines.
    """
    if not value or _whole_stand_in(value):
        return False
    compact = _PEM_ARMOR_RX.sub("", re.sub(r"\s+", "", value))
    if _OPAQUE_KEY_RX.fullmatch(compact) and not _whole_stand_in(compact):
        return True
    # value.strip() leaves newlines inside a wrapped hex key. compact has
    # already removed every whitespace character.
    return _COLON_HEX_RX.fullmatch(compact) is not None


def _jwk_key_lines(text: str) -> dict[int, str]:
    """Lines (1-based, in the rule's splitlines model) covered by a private
    member, as `private_members` classifies it, whose value looks like real
    key material (see above), each mapped to the name of the first such
    member on it: the name a HIGH finding on that line reports (#2323)."""
    members = []
    for m in private_members(text, coarse=False):
        if not (is_private_member(ANY_KEY_TYPE, m.name) or m.name in _OTH_PRIVATE):
            continue
        if _jwk_live_material(_jwk_value(m.value)):
            members.append(m)
    if not members:
        return {}
    starts = list(itertools.accumulate((len(ln) for ln in text.splitlines(True)), initial=0))
    lines: dict[int, str] = {}
    for member in members:
        low, high = jwk_audit_span(member, text)
        first = bisect.bisect_right(starts, low)
        last = bisect.bisect_right(starts, max(low, high - 1))
        for line_no in range(first, last + 1):
            lines.setdefault(line_no, member.name)
    return lines


def _jwk_member_sources(text: str) -> dict[int, list[str | None]]:
    """The value of every member `check_secrets` reports, by covered line:
    the JWK values the operator baseline hashes (#2323). None stands for a
    member with no value of its own (a whole expression), so its finding is
    bound to the whole line instead."""
    members = [m for m in private_members(text, coarse=False) if not _is_exact_placeholder(m.value)]
    if not members:
        return {}
    starts = list(itertools.accumulate((len(ln) for ln in text.splitlines(True)), initial=0))
    lines: dict[int, list[str | None]] = defaultdict(list)
    for member in members:
        low, high = jwk_audit_span(member, text)
        first = bisect.bisect_right(starts, low)
        last = bisect.bisect_right(starts, max(low, high - 1))
        for line_no in range(first, last + 1):
            lines[line_no].append(member.value if member.value.strip() else None)
    return lines


def _finding_values(line: str, jwk_sources: list[str | None]) -> frozenset[tuple[str, str]]:
    """(baseline rule, sha256) of every value on a SECRETS-KEY finding's line:
    each secret-pattern match, raw or in the decoded payload, and each JWK
    member covering it (the whole line for a member without its own value).
    A finding is accepted only when all of them are."""
    values = {
        (f"{audit_baseline.RULE_PREFIX}{name}", audit_baseline.value_hash(value))
        for name, value in _line_tokens(line)
    }
    for name, rx in _SECRET_SHAPES:
        for m in rx.finditer(line):
            value = m.groupdict().get("val") or m.group(0)
            values.add((f"{audit_baseline.RULE_PREFIX}{name}", audit_baseline.value_hash(value)))
    values.update(
        (audit_baseline.JWK_RULE, audit_baseline.value_hash(line if s is None else s))
        for s in jwk_sources
    )
    return frozenset(values)


_LOCAL_REPO_HISTORY_FIX = (
    "if the value is a live credential, rotate it and delete or redact the "
    "working file; the store's local git repo keeps committed copies, so purge "
    "that history too; a fixture or test vector the agent read needs no action"
)


def _agent_log_secret(
    finding: Finding, kind: str, line: str, jwk_lines, *, local_history: bool = False
) -> Finding:
    """Grade one SECRETS-KEY finding in the agent-log class (see above).
    `jwk_lines` is called only for a JWK finding: the scan is not repeated
    for a file without one. ``local_history`` is a store whose own git repo
    retains committed copies after the working file is deleted."""
    restated = transcript_secret_finding(finding)
    if local_history:
        restated = replace(restated, fix=_LOCAL_REPO_HISTORY_FIX)
    jwk = finding.message.startswith(JWK_MEMBER_MESSAGE_PREFIX)
    if jwk and kind == "transcript":
        return restated  # the #2157 JWK downgrade, unchanged: low
    if not jwk:
        if _line_holds_token(line):
            return replace(restated, severity="high")
        return replace(restated, severity=AGENT_LOG_SECRET_SEVERITY)
    # A JWK finding is one per line, named after the first member the scanner
    # masked there, which on a JSONL record is often a record field or a
    # non-private name ("kty", "value", "command"). It is HIGH only for what
    # its line really holds, and then names that (#2323): a live private
    # member, else a token found in the decoded payload.
    live = jwk_lines().get(finding.line)
    if live is not None:
        return replace(
            restated,
            severity="high",
            message=f'{JWK_MEMBER_MESSAGE_PREFIX}"{live}"{TRANSCRIPT_SUFFIX}',
        )
    tokens = _line_tokens(line)
    if tokens:
        return replace(
            restated,
            severity="high",
            message=f"possible {tokens[0][0]}{TRANSCRIPT_SUFFIX} (in a decoded payload)",
        )
    return replace(restated, severity=AGENT_LOG_SECRET_SEVERITY)


def _ancestor_dirs(path: Path) -> list[Path]:
    """Directories a reader must search to reach ``path``: parent first, up to
    the filesystem root, so a store under an owner-only home is unreachable."""
    return list(path.resolve().parents)


def _ancestor_modes(path: Path) -> list[int]:
    """Search-bit modes of the directories above ``path``, as seen by users
    other than the store's owner. A directory owned by someone else is
    searchable by that owner, who is such a user, so it never blocks."""
    try:
        owner = path.stat().st_uid
    except OSError:
        owner = None
    modes = []
    for directory in _ancestor_dirs(path):
        try:
            st = directory.stat()
        except OSError:
            continue  # unknown: assume traversable rather than hide a finding
        modes.append(st.st_mode if owner is None or st.st_uid == owner else 0o011)
    return modes


# What the next commit records is the index, not the working copy (#2225).
# The pre-commit hook audits the whole tree, so in every git work tree the
# engine takes one snapshot of the index before reading any file:
#   - a path in the snapshot is tracked, so it is never relaxed as a
#     transcript or credential store;
#   - every index blob is covered (the working-copy scan read exactly those
#     bytes: same object id, hashed from the bytes it read) or fetched by id,
#     checked against that id, and scanned. Git's own idea of a clean working
#     copy is not used (assume-unchanged, skip-worktree, clean filters and
#     symlinks all make it wrong). Fetches share one MAX_FILES budget:
#       Tier A, what the commit adds relative to HEAD (everything with no
#       HEAD, every conflict stage), must be read in full;
#       Tier B, committed blobs equal to HEAD whose working copy was not read,
#       uses the rest in path order. HEAD membership is no proof a blob was
#       ever audited at full severity, so Tier B is scanned, not skipped;
#   - the index is read again after the scan; any difference fails closed.
# Anything that leaves Tier A unproven (a git failure or timeout, a missing
# or mismatched object, more than MAX_FILES blobs, an index that changed) is a
# high AUDIT-INDEX-INCOMPLETE finding, so a gating run fails. Every Tier B
# blob left unread (over the budget, over the size cap while its working copy
# is not itself an oversized file, or after a git failure there) is counted
# in a medium AUDIT-INDEX-PARTIAL finding and marks the report truncated: an
# incomplete scan is reported, not a gate failure. Blobs over the size cap are
# otherwise skipped, as working files are. Fetched blobs are scanned one at a
# time and dropped.
#
# The audited root's own repository is read with the binding git itself
# resolves for the caller (`_caller_binding`: rev-parse with absolute paths,
# asked before the audit runs git anywhere else), and that one resolved binding
# goes to every git call in the audit path, engine and rules alike
# (rules.AUDIT_GIT_ENV). GIT_INDEX_FILE is how `git commit -a` and `git commit
# <path>` hand the hook the index they will commit, and GIT_DIR/GIT_WORK_TREE
# can bind a directory that has no `.git`. A repository nested below it is read
# with its own repository, not the outer hook's. Every git process runs
# isolated from system, global and transient caller config (`_isolate`), so
# caller config cannot steer a result. Replacement refs are ignored,
# so an object id always names its own bytes; fsmonitor is off so an audit
# never starts a configured monitor; pathspecs are literal so a name is never
# a glob.
_GIT_READ = (
    "git",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "--literal-pathspecs",
)
_GIT_TIMEOUT = 30
_GITLINK_MODE = "160000"
# `git rev-parse --local-env-vars`: what ties a git call to one repository.
_GIT_LOCAL_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)
# The variables that bind git to a repository's paths; when any is set the
# audit asks git to resolve them from the caller's directory (#2225).
_GIT_REPO_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)
_BLOB_BATCH_BYTES = 4 << 20
INDEX_RULE = "AUDIT-INDEX-INCOMPLETE"
PARTIAL_RULE = "AUDIT-INDEX-PARTIAL"
STAGED_SUFFIX = " (staged in the git index; the working copy differs)"


class _GitFailure(Exception):
    """A git read failed, timed out or returned something unexpected."""


def _git_run(
    tree: Path, args: tuple[str, ...], env: dict | None, stdin: bytes | None = None
) -> subprocess.CompletedProcess:
    """Run one read-only git command, isolated from caller config."""
    return _git_exec(tree, args, _isolate(os.environ if env is None else env), stdin)


def _git_run_with_caller_config(tree: Path, args: tuple[str, ...], env: dict):
    """`_git_run` without isolation. Only `_caller_binding` uses it, to fail
    closed when transient caller config would change the binding."""
    return _git_exec(tree, args, dict(env), None)


def _git_exec(
    tree: Path, args: tuple[str, ...], env: dict, stdin: bytes | None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [*_GIT_READ, *args],
            cwd=tree,
            input=stdin,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise _GitFailure(f"`git {args[0]}` timed out") from exc
    except OSError as exc:
        raise _GitFailure(f"`git {args[0]}` could not run: {exc.strerror or exc}") from exc


def _git_out(tree: Path, args: tuple[str, ...], env: dict | None, stdin=None) -> bytes:
    res = _git_run(tree, args, env, stdin)
    if res.returncode != 0:
        raise _GitFailure(f"`git {args[0]}` exited {res.returncode}")
    return res.stdout


def _blob_id(data: bytes, like: str) -> str | None:
    """The git object id of `data` as a blob, in the hash `like` uses."""
    algo = {40: "sha1", 64: "sha256"}.get(len(like))
    if algo is None:
        return None
    return hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest()


class _IndexSnapshot:
    """One work tree's index, read once before the scan (see above)."""

    def __init__(self, tree: Path, pathspecs: list[str], env: dict | None) -> None:
        self.tree = tree
        self.pathspecs = pathspecs
        self.env = env
        self.error: str | None = None
        self.paths: set[str] = set()
        # (git path, blob id) of every index entry: what is covered or fetched.
        self.added: list[tuple[str, str]] = []
        # Tier A: the entries this commit adds relative to HEAD (every entry
        # with no HEAD, every conflict stage). They must all be covered.
        self.new: set[tuple[str, str]] = set()
        # Tier A blobs over the size cap whose working copy is not the same.
        self.unread_new = 0
        self._raw = b""
        try:
            self._read()
        except _GitFailure as exc:
            self.error = str(exc)

    def _ls_files(self) -> bytes:
        return _git_out(self.tree, ("ls-files", "-s", "-z", "--", *self.pathspecs), self.env)

    def _read(self) -> None:
        self._raw = self._ls_files()
        stage0: dict[str, str] = {}
        for record in self._raw.split(b"\0"):
            if not record:
                continue
            meta, tab, raw_path = record.partition(b"\t")
            parts = meta.decode("ascii", "replace").split()
            if not tab or len(parts) != 3:
                raise _GitFailure("`git ls-files` returned an unexpected record")
            path = os.fsdecode(raw_path)
            self.paths.add(path)
            if parts[0] == _GITLINK_MODE:  # a submodule's content is its own repo's
                continue
            self.added.append((path, parts[1]))
            if parts[2] == "0":
                stage0[path] = parts[1]
            else:  # a conflict: every stage's content is in the index
                self.new.add((path, parts[1]))
        head = _git_run(self.tree, ("rev-parse", "-q", "--verify", "HEAD^{commit}"), self.env)
        if head.returncode == 1:  # unborn branch: the first commit adds everything
            self.new.update(stage0.items())
            return
        if head.returncode != 0:
            raise _GitFailure(f"`git rev-parse HEAD` exited {head.returncode}")
        out = _git_out(
            self.tree,
            (
                "diff-index",
                "--cached",
                "--raw",
                "-z",
                "--no-renames",
                "--ignore-submodules",
                "HEAD",
                "--",
                *self.pathspecs,
            ),
            self.env,
        )
        fields = out.split(b"\0")
        # ":<HEAD mode> <index mode> <HEAD oid> <index oid> <status>" NUL <path> NUL
        for meta, raw_path in zip(fields[::2], fields[1::2], strict=False):
            parts = meta.decode("ascii", "replace").lstrip(":").split()
            if len(parts) < 5:
                raise _GitFailure("`git diff-index` returned an unexpected record")
            if parts[4].startswith(("D", "U")) or parts[1] == _GITLINK_MODE:
                continue  # nothing added, or a conflict already listed
            path = os.fsdecode(raw_path)
            if stage0.get(path) != parts[3]:
                raise _GitFailure("the index changed while it was read")
            self.new.add((path, parts[3]))

    def tracked(self, rel: str) -> bool | None:
        """Whether `rel` is in this index; None when it could not be read."""
        return None if self.error else rel in self.paths

    def unchanged(self) -> bool:
        """Read the index again: False (and an error) if it is not the same."""
        if self.error:
            return False
        try:
            if self._ls_files() == self._raw:
                return True
            self.error = "the index changed during the audit"
        except _GitFailure as exc:
            self.error = str(exc)
        return False


_BINDING_ARGS = (
    "rev-parse",
    "--path-format=absolute",
    "--is-inside-work-tree",
    "--show-toplevel",
    "--absolute-git-dir",
    "--git-common-dir",
    "--git-path",
    "index",
    "--git-path",
    "objects",
)
_CONTROL_BYTES = frozenset(range(32)) | {127}


def _binding_fields(
    start: Path, env: dict, *, isolate: bool = True
) -> tuple[list[str] | None, str | None]:
    """Run the binding query once and parse it strictly: (top, git dir,
    common dir, index, objects), or None when git names no work tree.

    `rev-parse` has no NUL-delimited output, so the answer must be exactly
    six newline-terminated fields and no field may hold a control byte. A
    newline in any path adds a field, so the answer is refused rather than
    shifted (#2225 review r5)."""
    try:
        if isolate:
            res = _git_run(start, _BINDING_ARGS, env)
        else:
            res = _git_run_with_caller_config(start, _BINDING_ARGS, env)
    except _GitFailure as exc:
        return None, str(exc)
    out = res.stdout
    if res.returncode != 0:
        if out.startswith(b"false\n"):
            return None, None  # no work tree (a bare repository)
        return None, f"`git rev-parse` exited {res.returncode} resolving the repository"
    fields = out[:-1].split(b"\n") if out.endswith(b"\n") else []
    if len(fields) != 6 or fields[0] not in (b"true", b"false"):
        return None, "git reported a repository path with a newline in it"
    if any(_CONTROL_BYTES.intersection(field) for field in fields[1:]):
        return None, "git reported a repository path with a control character in it"
    if not all(fields[1:]):
        return None, "git reported an empty repository path"
    return [os.fsdecode(field) for field in fields[1:]], None


def _caller_binding(base: Path) -> tuple[dict, Path | None, bool, str | None]:
    """The repository an audit of `base` is about, as git itself resolves it
    (#2225 review r4, r5), with every path absolute.

    Git resolves GIT_DIR and GIT_WORK_TREE against its starting directory.
    With GIT_DIR and no GIT_WORK_TREE the top level is `core.worktree`, or
    else that directory. A persisted `core.worktree` also moves the top level
    with no variables set at all. A relative GIT_INDEX_FILE is resolved
    against the top level, and a relative GIT_COMMON_DIR or
    GIT_OBJECT_DIRECTORY is checked from the starting directory and used from
    the top level. Rather than re-derive those rules, the audit asks git once
    (`_binding_fields`), before it runs git anywhere else, and pins the answer
    for every later call. Caller config cannot steer the answer, because the
    query runs isolated (`_isolate`); if transient caller config would change
    it, the run fails closed instead.

    The query starts in the caller's directory when GIT_DIR or GIT_WORK_TREE
    binds the repository. Otherwise it starts there only if that directory
    lies in `base`'s repository, and in `base` if not. Outside any
    repository nothing is bound.

    Returns (environment, top level, whether a binding variable is set,
    error). An error fails the gate: git cannot say, the answer is
    ambiguous, a named index is missing, or an explicit GIT_DIR/GIT_WORK_TREE
    binds a repository other than `base`'s."""
    env = dict(os.environ)
    vars_set = any(env.get(k) for k in _GIT_REPO_VARS)
    explicit = bool(env.get("GIT_DIR") or env.get("GIT_WORK_TREE"))
    try:
        cwd: Path | None = Path(os.getcwd())
    except OSError:
        cwd = None
    if explicit:
        if cwd is None:
            return (
                env,
                None,
                True,
                "GIT_DIR/GIT_WORK_TREE are set but there is no current directory",
            )
        start = cwd
    else:
        layout = _GitLayout()
        tree = layout.work_tree(base)
        if tree is None:
            return env, None, False, None
        start = cwd if cwd is not None and layout.work_tree(cwd) == tree else base
    fields, error = _binding_fields(start, env)
    if error:
        return env, None, vars_set, error
    if fields is None:
        if explicit:
            return env, None, True, "GIT_DIR/GIT_WORK_TREE bind a repository with no work tree"
        return env, None, vars_set, None
    if any(k.startswith("GIT_CONFIG") for k in env):
        transient, _ = _binding_fields(start, env, isolate=False)
        if transient != fields:
            return env, None, vars_set, "transient git config changes which repository is bound"
    top, git_dir, common_dir, index, objects = fields
    resolved = {**env, "GIT_DIR": git_dir, "GIT_WORK_TREE": top}
    for key, value in (
        ("GIT_INDEX_FILE", index),
        ("GIT_COMMON_DIR", common_dir),
        ("GIT_OBJECT_DIRECTORY", objects),
    ):
        if env.get(key):
            resolved[key] = value
    if env.get("GIT_INDEX_FILE") and not (os.path.isfile(index) and os.access(index, os.R_OK)):
        # Git reads a missing index as empty; for an audit that would vouch
        # for a commit it never saw.
        return (
            resolved,
            None,
            True,
            "GIT_INDEX_FILE names an index that does not exist or cannot be read",
        )
    top_path = Path(top).resolve()
    if not base.is_relative_to(top_path):
        if explicit:
            return (
                resolved,
                None,
                True,
                "GIT_DIR/GIT_WORK_TREE bind a work tree that does not contain the audited "
                "root, so this run cannot vouch for that repository's commit",
            )
        return env, None, vars_set, None
    return resolved, top_path, vars_set, None


def _isolate(env) -> dict:
    """`env` with every caller config source neutralized (#2225 review r5):
    no system or global config and no transient `-c` parameters, so URL
    rewrites, excludes files and similar settings cannot steer an audit git
    call. The repository's own config still applies, as it does for git."""
    isolated = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG")}
    isolated["GIT_CONFIG_NOSYSTEM"] = "1"
    isolated["GIT_CONFIG_GLOBAL"] = os.devnull
    return isolated


def _git_env(tree: Path | None, own_tree: Path | None, own_env: dict) -> dict:
    """The isolated environment for a git call in `tree`: the caller's
    binding, resolved, for the tree it describes; otherwise one without the
    variables that bind git to a repository, so a nested repository is read
    with its own."""
    if tree is not None and tree == own_tree:
        return _isolate(own_env)
    return _isolate({k: v for k, v in own_env.items() if k not in _GIT_LOCAL_ENV})


def _walk_skips(base: Path, relpath: str) -> bool:
    """Whether `_iter_files` would never reach `relpath` (a skipped directory)."""
    chain = [base.name, *relpath.split("/")[:-1]]
    return any(
        d in SKIP_DIRS or (parent, d) in SKIP_NESTED_DIRS
        for parent, d in zip(chain, chain[1:], strict=False)
    )


def _staged_pathspecs(tree: Path, root: Path, single_file: bool) -> list[str]:
    """Limit an index query to the audited root (none: the whole tree)."""
    if tree == root or not (single_file or root.is_relative_to(tree)):
        return []
    return [root.relative_to(tree).as_posix()]


def _tree_label(tree: Path, base: Path) -> str | None:
    # A nested tree is named by its path; the tree holding the audited root
    # (at or above it, as for a single file) is the report's own repo.
    if tree != base and tree.is_relative_to(base):
        return tree.relative_to(base).as_posix()
    return None


def _index_finding(tree: Path, base: Path, reason: str) -> Finding:
    """High: what the next commit adds could not all be audited."""
    rel = _tree_label(tree, base)
    where = f"`{rel}`" if rel else "this work tree"
    return Finding(
        rule=INDEX_RULE,
        severity="high",
        message=(
            f"the git index of {where} could not be fully audited: {reason}; "
            "the next commit may record content this audit did not scan"
        ),
        fix=(
            "make sure git can read the repository and nothing else is staging, then "
            "audit again; stage fewer files per commit if the blob limit was reached"
        ),
        path=rel,
    )


def _partial_finding(tree: Path, base: Path, count: int, reason: str) -> Finding:
    """Medium: `count` committed blobs (equal to HEAD, working copy not read)
    were not re-scanned this run. Nothing records whether they were ever
    audited, so the notice claims no history (#2225 review r3)."""
    rel = _tree_label(tree, base)
    where = f"`{rel}`" if rel else "this work tree"
    return Finding(
        rule=PARTIAL_RULE,
        severity="medium",
        message=(
            f"{count} committed blobs in {where} (equal to HEAD, working copy not read) "
            f"were not re-scanned this run: {reason}; CI audits a fresh checkout of the "
            "head commit with the same caps"
        ),
        fix=(
            "restore the hidden working copies, or audit a fresh checkout of the commit, "
            "to read them; a tree over the audit's file caps is covered only in part by "
            "any run"
        ),
        path=rel,
    )


def _blob_sizes(snapshot: _IndexSnapshot, oids: list[str]) -> dict[str, int]:
    """Sizes of `oids` from one `cat-file --batch-check`; raise `_GitFailure`
    if any object is missing or is not a blob."""
    out = _git_out(
        snapshot.tree,
        ("cat-file", "--batch-check"),
        snapshot.env,
        "".join(f"{o}\n" for o in oids).encode(),
    )
    sizes: dict[str, int] = {}
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
            raise _GitFailure(f"a staged object is missing or not a blob: {line[:80]}")
        sizes[parts[0]] = int(parts[2])
    if set(sizes) != set(oids):
        raise _GitFailure("`git cat-file --batch-check` did not answer for every blob")
    return sizes


def _iter_blobs(snapshot: _IndexSnapshot, oids: list[str], sizes: dict[str, int]):
    """Yield (oid, bytes) for `oids`, read in `--batch` chunks of about
    _BLOB_BATCH_BYTES, so only one chunk is held at a time. Each blob must
    hash to the id that was asked for; raise `_GitFailure` otherwise."""
    i = 0
    while i < len(oids):
        chunk, total = [], 0
        while i < len(oids) and (not chunk or total + sizes[oids[i]] <= _BLOB_BATCH_BYTES):
            chunk.append(oids[i])
            total += sizes[oids[i]]
            i += 1
        out = _git_out(
            snapshot.tree,
            ("cat-file", "--batch"),
            snapshot.env,
            "".join(f"{o}\n" for o in chunk).encode(),
        )
        pos = 0
        for oid in chunk:
            end = out.find(b"\n", pos)
            header = out[pos:end].decode("ascii", "replace").split() if end >= 0 else []
            if header != [oid, "blob", str(sizes[oid])]:
                raise _GitFailure("`git cat-file --batch` returned an unexpected object")
            start = end + 1
            pos = start + sizes[oid] + 1
            if len(out) < pos:
                raise _GitFailure("`git cat-file --batch` returned a short object")
            data = out[start : pos - 1]
            if _blob_id(data, oid) != oid:
                raise _GitFailure(f"object {oid[:12]} does not hash to its id")
            yield oid, data
        del out


def _scan_blob_text(relpaths: list[str], text: str, seen: set, findings: list[Finding]) -> None:
    lines = text.splitlines()
    for relpath in relpaths:
        for rule in CONTENT_RULES:
            for finding in rule(relpath, text) or ():
                if _suppressed(finding, lines):
                    continue
                key = (finding.rule, finding.path, finding.line, finding.message)
                if key in seen:
                    continue
                seen.add(key)
                finding.message = f"{finding.message}{STAGED_SUFFIX}"
                findings.append(finding)


def _at_outer_mcp_path(relpath: str) -> bool:
    """Whether `relpath` ends in an MCP config path, as an enclosing
    repository's config may be a deeper repository's file (#2263)."""
    return any(relpath.endswith(f"/{rel}") for rel in MCP_CONFIG_PATHS)


def _fetch_and_scan(
    snapshot: _IndexSnapshot,
    base: Path,
    by_oid: dict[str, list[str]],
    sizes: dict[str, int],
    seen: set,
    findings: list[Finding],
    done: set[str] | None = None,
    staged_texts: dict[str, str] | None = None,
) -> None:
    """Fetch the blobs in `by_oid` one bounded chunk at a time and scan each
    as it arrives, adding its id to `done`; raise `_GitFailure` as
    `_iter_blobs` does. The text of a blob at a path a content-reading repo
    rule reads (STAGED_CONFIG_PATHS) is kept in `staged_texts`, so those
    rules judge the staged bytes too (#2225 review r5)."""
    for oid, data in _iter_blobs(snapshot, sorted(by_oid), sizes):
        if b"\x00" not in data[:8192]:  # a binary blob is not scanned
            text = data.decode(errors="replace")
            del data
            _scan_blob_text(by_oid[oid], text, seen, findings)
            if staged_texts is not None:
                for relpath in by_oid[oid]:
                    repo_rel = (base / relpath).relative_to(snapshot.tree).as_posix()
                    if (
                        relpath in STAGED_CONFIG_PATHS
                        or repo_rel in MCP_CONFIG_PATHS
                        or _at_outer_mcp_path(relpath)
                    ):
                        staged_texts[relpath] = text
        if done is not None:
            done.add(oid)


def _scan_staged_blobs(
    snapshots: list[_IndexSnapshot],
    covered: set[tuple[Path, str]],
    base: Path,
    excludes: list[str],
    findings: list[Finding],
    excluded_files: set[str],
    staged_texts: dict[str, str] | None = None,
) -> tuple[str | None, list[tuple[_IndexSnapshot, int, str]]]:
    """Fetch and scan, at full severity, the index blobs the working-copy
    scan did not read, in two tiers sharing one budget of MAX_FILES blobs
    within the size cap (a path in the index is tracked, so neither
    relaxation applies; a finding the working copy already reported is not
    repeated):

    - Tier A, what the commit adds relative to HEAD, must be read in full.
      A git failure marks the snapshot (`error`: the gate fails), and so does
      a blob over the size cap whose working copy is not the same bytes
      (`unread_new`); more than MAX_FILES blobs is returned as a reason (the
      first MAX_FILES are read).
    - Tier B, committed blobs equal to HEAD whose working copy was not read
      (cleaned, deleted, hidden), uses what budget is left, in path order.
      Every Tier B path left unread (over the budget, over the size cap with
      no oversized working copy, or after a git failure) is returned as a
      (snapshot, count, reason) notice. Notices do not fail the gate.
    """
    seen = {(fd.rule, fd.path, fd.line, fd.message) for fd in findings}
    tiers: tuple[dict, dict] = ({}, {})  # snapshot -> oid -> relpaths
    for snapshot in snapshots:
        if snapshot.error:
            continue
        for rel, oid in snapshot.added:
            path = snapshot.tree / rel
            if (path, oid) in covered:
                continue
            try:
                relpath = path.relative_to(base).as_posix()
            except ValueError:
                continue
            if _walk_skips(base, relpath):
                continue
            if _is_excluded(relpath, excludes):
                excluded_files.add(relpath)
                continue
            tier = tiers[0 if (rel, oid) in snapshot.new else 1]
            tier.setdefault(snapshot, {}).setdefault(oid, []).append(relpath)
    limit = config.SEARCH_FILE_MAX_BYTES

    # Tier A: everything, or the gate fails.
    wanted_a = 0
    budget = MAX_FILES
    for snapshot, by_oid in tiers[0].items():
        try:
            sizes = _blob_sizes(snapshot, sorted(by_oid))
            fetch: dict[str, list[str]] = {}
            for oid in sorted(by_oid):
                if sizes[oid] > limit:
                    # Over the size cap. Only identical bytes in the working
                    # copy share a file's size exemption; anything else is
                    # new content this run cannot read (#2225 review r5).
                    snapshot.unread_new += sum(
                        1
                        for relpath in by_oid[oid]
                        if not _working_copy_matches(base / relpath, oid, sizes[oid])
                    )
                    continue
                wanted_a += len(by_oid[oid])
                if budget > 0:
                    fetch[oid] = by_oid[oid]
                    budget -= len(by_oid[oid])
            _fetch_and_scan(snapshot, base, fetch, sizes, seen, findings, staged_texts=staged_texts)
            if snapshot.unread_new:
                snapshot.error = (
                    f"{snapshot.unread_new} new staged blobs over the {limit}-byte size cap "
                    "differ from their working copy and were not read"
                )
        except _GitFailure as exc:
            snapshot.error = str(exc)
    overflow = None
    if wanted_a > MAX_FILES:
        overflow = f"{wanted_a} new or changed staged blobs to read, over the {MAX_FILES} limit"
    budget = max(0, MAX_FILES - wanted_a)

    # Tier B: committed content, deterministic by path, within what is left.
    # Every blob left unread, for any reason, is counted in a notice.
    notices: list[tuple[_IndexSnapshot, int, str]] = []
    candidates: list[tuple[str, _IndexSnapshot, str]] = []
    sizes_b: dict[_IndexSnapshot, dict[str, int]] = {}
    for snapshot, by_oid in tiers[1].items():
        if snapshot.error:
            continue  # already a gate failure for this tree
        try:
            sizes_b[snapshot] = _blob_sizes(snapshot, sorted(by_oid))
        except _GitFailure as exc:
            notices.append((snapshot, sum(map(len, by_oid.values())), str(exc)))
            continue
        hidden_big = 0
        for oid, relpaths in by_oid.items():
            if sizes_b[snapshot][oid] <= limit:
                candidates.extend((relpath, snapshot, oid) for relpath in relpaths)
                continue
            # Over the size cap. When the working copy holds the same bytes,
            # a file's size exemption covers both; otherwise the committed
            # bytes are hidden content this run did not read.
            hidden_big += sum(
                1
                for relpath in relpaths
                if not _working_copy_matches(base / relpath, oid, sizes_b[snapshot][oid])
            )
        if hidden_big:
            notices.append((snapshot, hidden_big, f"over the {limit}-byte size cap"))
    candidates.sort(key=lambda item: item[0])
    over_budget: dict[_IndexSnapshot, int] = defaultdict(int)
    for _, snapshot, _ in candidates[budget:]:
        over_budget[snapshot] += 1
    for snapshot, count in over_budget.items():
        notices.append((snapshot, count, f"over the {MAX_FILES}-blob limit"))
    chosen: dict[_IndexSnapshot, dict[str, list[str]]] = {}
    for relpath, snapshot, oid in candidates[:budget]:
        chosen.setdefault(snapshot, {}).setdefault(oid, []).append(relpath)
    for snapshot, by_oid in chosen.items():
        done: set[str] = set()
        try:
            _fetch_and_scan(
                snapshot, base, by_oid, sizes_b[snapshot], seen, findings, done, staged_texts
            )
        except _GitFailure as exc:
            left = sum(len(relpaths) for oid, relpaths in by_oid.items() if oid not in done)
            notices.append((snapshot, left, str(exc)))
    return overflow, notices


def _working_copy_matches(path: Path, oid: str, size: int) -> bool:
    """Whether `path` is a regular file holding exactly the blob `oid` of
    `size` bytes, hashed as git hashes it. Only then does an unread blob over
    the size cap share the working file's size exemption."""
    algo = {40: "sha1", 64: "sha256"}.get(len(oid))
    if algo is None:
        return False
    try:
        if os.stat(path).st_size != size:
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            return False
        digest = hashlib.new(algo, b"blob %d\0" % size)
        read = 0
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
            read += len(chunk)
    except OSError:
        return False
    finally:
        os.close(fd)
    return read == size and digest.hexdigest() == oid


def _suppressed(finding, lines: list[str]) -> bool:
    if finding.line is None or finding.line > len(lines):
        return False
    return SUPPRESS_MARKER in lines[finding.line - 1]


def _is_excluded(relpath: str, excludes: list[str]) -> bool:
    for pattern in excludes:
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        # 1. Exact match or prefix directory match (e.g. dir/ or dir)
        clean_pat = pattern[:-1] if pattern.endswith("/") else pattern
        if (
            relpath == clean_pat
            or relpath.startswith(f"{clean_pat}/")
            or f"/{clean_pat}/" in f"/{relpath}"
        ):
            return True
        # 2. Glob matching for filename if there is no slash in the pattern
        if "/" not in pattern:
            if fnmatch.fnmatch(os.path.basename(relpath), pattern):
                return True
        # 3. Glob matching against the full relative path. Use PurePosixPath.match,
        #    which is segment-aware: '*' does NOT cross '/', so a pattern like
        #    "config/*.yaml" matches "config/prod.yaml" but not "config/sub/prod.yaml"
        #    and "test*" never leaks into "tests/secret.py". (Plain fnmatch treats
        #    '*' as matching '/', which would silently exclude — and hide findings
        #    for — files in unrelated subdirectories.)
        if PurePosixPath(relpath).match(pattern):
            return True
    return False


def run_audit(
    root: Path,
    remediate: bool = False,
    excludes: list[str] | None = None,
    include_repo_rules: bool = True,
    *,
    baseline: audit_baseline.Baseline | Path | None = None,
    collect_values: list | None = None,
) -> dict:
    """Audit `root` — a directory, or a single file — and return a JSON-safe report.

    {root, files_scanned, status, summary, findings, findings_total, truncated, truncated_by}; status is
    "fail" if any high finding, "warn" if any medium, else "pass".

    `include_repo_rules=False` skips REPO_RULES (.gitignore/lockfile/agent-settings/
    .mcp.json hygiene) — for callers auditing content that isn't a code project tree,
    e.g. a single note or a non-repo directory, where those checks don't apply.

    `baseline` (a loaded `audit.baseline.Baseline` or its file) grades each
    SECRETS-KEY finding whose every value the operator accepted for that file
    as info, unless the file is inside a git work tree, and adds
    `baseline: {path, entries, accepted, warning}` to the report. Only
    `surfaces` (by default) and the CLI's explicit `--baseline` pass it; the
    default changes nothing (#2323). `collect_values`, a list, receives
    {path, line, severity, values, in_git} for each SECRETS-KEY finding:
    hashes only, for `baseline accept`.
    """
    if isinstance(baseline, (str, os.PathLike)):
        baseline = audit_baseline.load(Path(baseline))
    # The run's git environment resolver, installed once the binding is known
    # and used by every git helper in the audit path (rules.AUDIT_GIT_ENV).
    git_scope: list = []
    token = AUDIT_GIT_ENV.set(lambda directory: git_scope[0](directory) if git_scope else None)
    caller_token = AUDIT_GIT_CALLER_ENV.set(
        lambda directory: git_scope[1](directory) if len(git_scope) > 1 else None
    )
    try:
        return _run_audit(
            root, remediate, excludes, include_repo_rules, git_scope, baseline, collect_values
        )
    finally:
        AUDIT_GIT_CALLER_ENV.reset(caller_token)
        AUDIT_GIT_ENV.reset(token)


def _run_audit(
    root: Path,
    remediate: bool,
    excludes: list[str] | None,
    include_repo_rules: bool,
    git_scope: list,
    baseline: audit_baseline.Baseline | None = None,
    collect_values: list | None = None,
) -> dict:
    root = root.resolve()
    # Value hashes are computed only when something will read them.
    want_values = collect_values is not None or bool(baseline and baseline.entries)
    finding_values: dict[int, tuple[Finding, frozenset]] = {}
    single_file = root.is_file()
    base = root.parent if single_file else root

    # Parse baseline and excludes
    excludes_list = list(excludes) if excludes else []
    # Read without blocking: a check-then-open could meet a FIFO (#2212).
    ignore_text = read_regular_text(base / ".ua-audit-ignore", errors="replace")
    if ignore_text is not None:
        for line in ignore_text.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                excludes_list.append(line)

    repos: set[Path] = set()  # directories holding a `.git` marker
    if single_file:
        files, files_truncated = [root], False
    else:
        files, files_truncated = _iter_files(root, excludes_list, repos)
    findings = []
    scanned = 0
    excluded_files = set()
    relaxed: list[tuple[Path, list[tuple[int | None, Finding]]]] = []

    # Gitignored files in the root's own repo (scratch dirs, caches) are not
    # documents anyone committed: the base64 rule is skipped there (#1846).
    caller_env, bound, env_set, env_error = _caller_binding(base)
    git = _GitLayout(bound)
    base_tree = git.work_tree(base)
    # The one tree read with the caller's git environment (GIT_INDEX_FILE,
    # GIT_DIR, ...); every other tree is read with its own repository.
    if bound is not None:
        own_tree = bound
    else:
        own_tree = None if env_set else base_tree

    def git_env_for(directory: Path) -> dict:
        return _git_env(git.work_tree(directory), own_tree, caller_env)

    def caller_env_for(directory: Path) -> dict:
        """The same binding with the caller's own config, for the one check
        that may only become stricter with it (rules.AUDIT_GIT_CALLER_ENV)."""
        if git.work_tree(directory) == own_tree and own_tree is not None:
            return dict(caller_env)
        # Drop the outer repository's binding, but retain transient config:
        # URL rewrites apply to nested repositories too. This view can only
        # prevent a downgrade; the isolated view must also prove local-only.
        return {
            k: v
            for k, v in caller_env.items()
            if k not in _GIT_LOCAL_ENV or k in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
        }

    # Every git helper of the run, in this module and in the rules, uses it.
    git_scope.append(git_env_for)
    git_scope.append(caller_env_for)
    ignored: set[str] = set()
    if base_tree is not None:
        ignored = git_ignored_paths(
            base,
            [f.relative_to(base).as_posix() for f in files if git.work_tree(f.parent) == base_tree],
        )

    # One index snapshot per work tree, before any file is read (#2225).
    trees = sorted(({git.work_tree(f.parent) for f in files} | repos | {base_tree}) - {None})
    snapshots = {
        tree: _IndexSnapshot(
            tree, _staged_pathspecs(tree, root, single_file), _git_env(tree, own_tree, caller_env)
        )
        for tree in trees
    }
    if env_error:
        findings.append(_index_finding(base, base, env_error))
    added: dict[Path, set[str]] = defaultdict(set)
    for snapshot in snapshots.values():
        for rel, oid in snapshot.added:
            added[snapshot.tree / rel].add(oid)
    covered: set[tuple[Path, str]] = set()  # (path, blob id) the scan read

    def untracked(path: Path) -> bool:
        """Relax only a file the snapshot positively shows out of the index."""
        tree = git.work_tree(path.parent)
        if tree is None:
            return True
        snapshot = snapshots.get(tree)
        return snapshot is not None and snapshot.tracked(path.relative_to(tree).as_posix()) is False

    local_repos: dict[Path, bool] = {}

    def enclosing_untracked(path: Path, tree: Path) -> bool:
        """Every repository enclosing `tree`, up to the filesystem root,
        positively lacks `path` in its index (snapshot when held, else git)."""
        outer = git.work_tree(tree.parent) if tree.parent != tree else None
        while outer is not None:
            rel = path.relative_to(outer).as_posix()
            snapshot = snapshots.get(outer)
            state = snapshot.tracked(rel) if snapshot is not None else _git_file_tracked(outer, rel)
            if state is not False:
                return False
            outer = git.work_tree(outer.parent) if outer.parent != outer else None
        return True

    def local_only(tree: Path) -> bool:
        """`_local_only_repo`, once per tree in a run."""
        if tree not in local_repos:
            local_repos[tree] = _local_only_repo(tree)
        return local_repos[tree]

    for f in files:
        relpath = f.relative_to(base).as_posix()
        if _is_excluded(relpath, excludes_list):
            excluded_files.add(relpath)
            continue
        data = read_regular_file(f, limit=config.SEARCH_FILE_MAX_BYTES)
        if data is None:
            continue
        for oid in added.get(f, ()):
            if _blob_id(data, oid) == oid:
                covered.add((f, oid))
        if b"\x00" in data[:8192]:
            continue
        text = data.decode(errors="replace")
        scanned += 1
        lines = text.splitlines()
        store = _is_local_credential_store(f, base, relpath, git) and untracked(f)
        # A detached signature over the document beside it is not source
        # (task #2419). Decided once per file, and only for the one name shape.
        signature = _signed_document(f, base, relpath, text, git)
        holds_secret = False
        agent_log: str | None | bool = False  # decided on the first SECRETS-KEY finding
        jwk_lines: list[dict[int, str]] = []  # computed on the first JWK finding there
        member_sources: list[dict[int, list[str]]] = []  # for baseline value hashes

        def key_lines(text=text, cache=jwk_lines) -> dict[int, str]:
            if not cache:
                cache.append(_jwk_key_lines(text))
            return cache[0]

        # Findings relaxed because the file was untracked, undone if the index
        # turns out to have changed (#2225): (position in `findings`, or None
        # if dropped; the original finding).
        file_relaxed: list[tuple[int | None, Finding]] = []
        for rule in CONTENT_RULES:
            for finding in rule(relpath, text) or ():
                if _suppressed(finding, lines):
                    continue
                if finding.rule == "INJECTION-BASE64" and relpath in ignored:
                    continue
                if signature is not None and finding.rule == "SECRETS-KEY":
                    finding = signature_secret_finding(finding)
                if store and finding.rule == "SECRETS-KEY":
                    holds_secret = True
                    file_relaxed.append((None, finding))
                    continue
                if finding.rule == "SECRETS-KEY":
                    if agent_log is False:
                        agent_log = _agent_log_class(
                            f, git, text, untracked, local_only, enclosing_untracked
                        )
                    if agent_log:
                        file_relaxed.append((len(findings), finding))
                        line = lines[finding.line - 1] if finding.line else ""
                        tree = git.work_tree(f.parent)
                        local_history = tree is not None and local_only(tree)
                        finding = _agent_log_secret(
                            finding, agent_log, line, key_lines, local_history=local_history
                        )
                    if want_values and finding.line and finding.line <= len(lines):
                        if not member_sources:
                            member_sources.append(_jwk_member_sources(text))
                        values = _finding_values(
                            lines[finding.line - 1], member_sources[0].get(finding.line, [])
                        )
                        finding_values[id(finding)] = (finding, values)
                findings.append(finding)
        if file_relaxed:
            relaxed.append((f, file_relaxed))
        # A local credential store's secret is its job; its mode is the risk.
        if store and (holds_secret or VENDOR_CREDENTIAL_STORE_RX.match(f.name)):
            try:
                mode = f.stat().st_mode
            except OSError:
                continue
            ancestors = _ancestor_modes(f) if mode & 0o044 else []
            findings.extend(check_credential_store_mode(relpath, mode, ancestors))

    # Staged blobs the scan did not read, then the closing index read: the
    # snapshot held for the whole audit, or every relaxation in that tree is
    # undone and the gate fails.
    staged_texts: dict[str, str] = {}
    overflow, notices = _scan_staged_blobs(
        list(snapshots.values()),
        covered,
        base,
        excludes_list,
        findings,
        excluded_files,
        staged_texts,
    )
    if any(snapshot.unread_new for snapshot in snapshots.values()):
        files_truncated = True
    for snapshot in snapshots.values():
        if snapshot.unchanged():
            continue
        for path, items in relaxed:
            if git.work_tree(path.parent) != snapshot.tree:
                continue
            for pos, original in items:
                if pos is None:
                    findings.append(original)
                else:
                    findings[pos] = original
        findings.append(_index_finding(snapshot.tree, base, snapshot.error or "unknown"))
    if overflow:
        files_truncated = True
        findings.append(_index_finding(base_tree or trees[0], base, overflow))
    for snapshot, count, reason in notices:
        if count and not snapshot.error:
            files_truncated = True
            findings.append(_partial_finding(snapshot.tree, base, count, reason))

    if include_repo_rules:
        for rule in REPO_RULES:
            findings.extend(rule(root, files) or ())
        nested_trees = [tree for tree in trees if tree != root and tree.is_relative_to(root)]
        for tree in nested_trees:
            for finding in check_mcp_config(tree, files) or ():
                if finding.path:
                    finding.path = (tree / finding.path).relative_to(base).as_posix()
                findings.append(finding)
        # The content-reading repo rules judge staged config blobs the
        # working copy did not hold, as the content rules do (#2225 r5).
        if staged_texts and not single_file:
            seen = {(fd.rule, fd.path, fd.line, fd.message) for fd in findings}
            contexts = [(root, STAGED_REPO_RULES, staged_texts)]
            for tree in nested_trees:
                prefix = tree.relative_to(base).as_posix() + "/"
                nested_texts = {
                    rel.removeprefix(prefix): text
                    for rel, text in staged_texts.items()
                    if rel.startswith(prefix)
                }
                contexts.append((tree, (check_mcp_config,), nested_texts))
            for rule_root, rules, texts in contexts:
                for rule in rules:
                    for finding in rule(rule_root, files, staged=texts) or ():
                        if finding.path:
                            finding.path = (rule_root / finding.path).relative_to(base).as_posix()
                        key = (finding.rule, finding.path, finding.line, finding.message)
                        if key not in seen:
                            seen.add(key)
                            finding.message = f"{finding.message}{STAGED_SUFFIX}"
                            findings.append(finding)

    # Filter out findings associated with excluded paths (e.g. from REPO_RULES)
    filtered_findings = []
    for fd in findings:
        if fd.path and (fd.path in excluded_files or _is_excluded(fd.path, excludes_list)):
            excluded_files.add(fd.path)
        else:
            filtered_findings.append(fd)
    findings = filtered_findings

    if remediate:
        from .remediate import remediate_findings

        remediate_findings(root, findings)

    # Drop byte-identical findings so overlapping rules can't inflate counts.
    # The message stays in the key because one rule may legitimately report
    # several distinct defects on the same line (e.g. AGENT-PERMISSIVE-CONFIG);
    # differently-worded cross-rule overlap is prevented at the source by rule
    # ownership (Python: check_py_ast, JS: check_llm_code, secrets:
    # check_secrets) and pinned by test_audit_reports_each_finding_once.
    seen = set()
    unique = []
    for fd in findings:
        key = (fd.rule, fd.path, fd.line, fd.message)
        if key not in seen:
            seen.add(key)
            unique.append(fd)
    findings = unique

    accepted = 0
    rotated: set[str] = set()  # files with an accepted older value and a new one (#2419)
    if want_values:
        for i, fd in enumerate(findings):
            item = finding_values.get(id(fd))
            if item is None or item[0] is not fd or not fd.path:
                continue  # a staged blob, or a relaxation undone: never accepted
            values = item[1]
            # Repo source is never accepted, even on request: only a file
            # outside every git work tree (an agent's own home) qualifies.
            in_git = git.work_tree((base / fd.path).parent) is not None
            if collect_values is not None:
                collect_values.append(
                    {
                        "path": fd.path,
                        "line": fd.line,
                        "severity": fd.severity,
                        "values": values,
                        "in_git": in_git,
                    }
                )
            entry = baseline.accepted(base / fd.path, values) if baseline and not in_git else None
            if entry is not None:
                accepted += 1
                findings[i] = replace(
                    fd,
                    severity="info",
                    message=f"{audit_baseline.accepted_note(entry)}: {fd.message}",
                    fix="reviewed in the operator audit baseline; a new value, another "
                    "rule or another file raises it again",
                )
            elif baseline and not in_git and baseline.holds_file(base / fd.path):
                rotated.add(fd.path)

    # Sort BEFORE capping. The cap used to slice in discovery order, so a run
    # over the cap could drop every high finding and keep info ones — the worst
    # possible thing for a security report to lose.
    findings.sort(key=lambda fd: (_SEV_RANK[fd.severity], fd.path or "", fd.line or 0))

    # Report how much was dropped, not merely that something was. A bare
    # `truncated: True` cannot distinguish losing one finding from losing a
    # thousand — task #198 was filed against a count that was really just the
    # cap, and the flag alone gave no way to notice.
    findings_total = len(findings)
    findings_truncated = findings_total > MAX_FINDINGS
    if findings_truncated:
        findings = findings[:MAX_FINDINGS]
    truncated_by = [
        name for name, hit in (("files", files_truncated), ("findings", findings_truncated)) if hit
    ]
    summary = {sev: 0 for sev in SEVERITIES}
    summary["excluded"] = len(excluded_files)
    for fd in findings:
        if not fd.fixed:
            summary[fd.severity] += 1
    status = "fail" if summary["high"] else ("warn" if summary["medium"] else "pass")
    truncated = bool(truncated_by)
    report = {
        "root": str(root),
        "files_scanned": scanned,
        "status": status,
        "grade": grade_from_summary(summary, truncated=truncated),
        "summary": summary,
        "findings": [fd.to_dict() for fd in findings],
        "findings_total": findings_total,
        "truncated": truncated,
        "truncated_by": truncated_by,
    }
    if baseline is not None:
        report["baseline"] = {
            "path": str(baseline.path),
            "entries": len(baseline.entries),
            "accepted": accepted,
            "rotated": sorted(rotated),
            "warning": baseline.warning,
        }
    return report


def format_text(report: dict) -> str:
    """Human-readable audit report (the CLI's text output; also used by merge)."""
    grade = report.get("grade", "")
    head = f"audit: {report['root']}  ({report['files_scanned']} files scanned)"
    if grade:
        head += f"  grade {grade}"
    out = [head]
    for fd in report["findings"]:
        loc = f"{fd['path']}:{fd['line']}" if fd.get("line") else (fd.get("path") or "(repo)")
        out.append(f"[{fd['severity'].upper():6}] {fd['rule']:24} {loc}")
        out.append(f"         {fd['message']}")
        out.append(f"         fix: {fd['fix']}")
    s = report["summary"]
    out.append(
        f"status: {report['status']}  "
        f"(high={s['high']} medium={s['medium']} low={s['low']} info={s['info']} excluded={s.get('excluded', 0)})"
    )
    if report["truncated"]:
        notes = []
        if "findings" in report.get("truncated_by", []):
            notes.append(
                f"showing {len(report['findings'])} of {report['findings_total']} findings"
            )
        if "files" in report.get("truncated_by", []):
            notes.append(f"file cap ({MAX_FILES}) reached — some files were never scanned")
        out.append("note: results truncated — " + "; ".join(notes))
    if report.get("baseline"):
        b = report["baseline"]
        out.append(f"baseline: accepted={b['accepted']} entries={b['entries']} ({b['path']})")
        if b.get("warning"):
            out.append(f"warning: {b['warning']}")
    return "\n".join(out)
