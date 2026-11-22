"""Audit rules. Each rule yields Findings; the engine decides which files it sees.

Content rules run per text file; repo rules run once against the whole tree.
A line containing ``ua:allow`` suppresses content-rule findings on that line.
"""

import base64
import binascii
import bisect
import codecs
import itertools
import json
import os
import re
import subprocess
import zlib
from collections import ChainMap
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from ..injection_patterns import _EXTRACT_VERBS, INJECTION_KEYWORDS, INJECTION_RX  # noqa: F401
from ..telemetry.redaction import CREDENTIAL_SHAPE_PATTERNS
from .safe_read import read_regular_text

SEVERITIES = ("high", "medium", "low", "info")

SUPPRESS_MARKER = "ua:allow"


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    fix: str
    path: str | None = None
    line: int | None = None
    fixed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _mask(secret: str) -> str:
    return f"{secret[:6]}…({len(secret)} chars)"


# --- secrets ------------------------------------------------------------------

SECRET_PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (
        "assigned secret literal",
        re.compile(
            r"""(?ix)\b(?:api_?key|secret|token|passwd|password|credential)\s*[:=]\s*
                ["'](?P<val>[A-Za-z0-9+/=_\-]{16,})["']"""
        ),
    ),
]

# Shared credential shapes (telemetry/redaction.py) extend the list above. The
# private-key shape is left to check_private_key, which owns that rule; short
# Google OAuth fixtures (ya29.) are deliberate test inputs for the gcloud
# scrubber, so that shape stays with redaction and the public relay. Query-string
# credentials (``?sig=``, ``?token=``) are runtime text to redact, not a source
# pattern: URLs with placeholder query values are routine in tests and docs.
_AUDIT_EXCLUDED_SHAPES = frozenset({"private key", "Google OAuth token", "credential in URL query"})
_SHARED_SECRET_SHAPES = tuple(
    (name, rx) for name, rx in CREDENTIAL_SHAPE_PATTERNS if name not in _AUDIT_EXCLUDED_SHAPES
)

PLACEHOLDER_HINTS = ("example", "your", "placeholder", "changeme", "dummy", "xxxx", "redacted")

# Documentation shapes that stand in for a credential rather than being one
# (task #1846): a proxy URL whose userinfo is the words user and pass, an angle-bracket
# slot (`https://<token>@github.com`), a variable reference (`${TOKEN}`,
# `$TOKEN`, `%TOKEN%`), a mask (`****`), or a password that repeats the
# user name (`postgres:postgres@localhost`, a local dev default).
_PLACEHOLDER_WORDS = frozenset(
    {"pass", "password", "passwd", "passphrase", "pwd", "pw", "secret", "token", "apikey"}
)
_PLACEHOLDER_SHAPE_RX = re.compile(
    # Every shape counts only as the whole value: a real password may contain
    # `$`, `${...}` or `<...>` (`Pa$sw0rd`, `hunter${2}x`), and keeps its
    # finding (PR #1000 review).
    r"^<[^<>\s]+>$|^\$\{[^}]*\}$|^\$[A-Za-z_][A-Za-z0-9_]*$|^%[A-Za-z_][A-Za-z0-9_]*%$|^[*x.•]+$",
    re.IGNORECASE,
)


_URL_USER_RX = re.compile(r"://([^\s/:@]+):")


def _is_placeholder(value: str, matched: str = "") -> bool:
    low = value.lower()
    user = _URL_USER_RX.search(matched)
    return (
        any(h in low for h in PLACEHOLDER_HINTS)
        or low.strip("\"'") in _PLACEHOLDER_WORDS
        or bool(_PLACEHOLDER_SHAPE_RX.search(value.strip("\"'")))
        or bool(user and user.group(1).lower() == low)
    )


_EXACT_PLACEHOLDER_RX = re.compile(r"\[?REDACTED(?:_[A-Z]+)*\]?")
# Only short literal labels made from known placeholder words count here
# (task #2213). A marker elsewhere in a label must not exempt opaque key
# material. Raw escapes, nested brackets and line separators retain findings.
_STAND_IN_MARKERS = frozenset({"fake", "dummy", "placeholder", "example"})
_STAND_IN_WORDS = _STAND_IN_MARKERS | {
    "a",
    "runtime",
    "synthetic",
    "test",
    "key",
    "value",
    "token",
    "secret",
}


def _is_marked_stand_in(value: str) -> bool:
    if not (value.startswith("<") and value.endswith(">")):
        return False
    label = value[1:-1]
    if "\\" in label or any(char in label for char in "<>\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        return False
    # Limit the split as well as the accepted word count so a long malformed
    # label cannot allocate an unbounded list of tiny words.
    words = label.lower().split(maxsplit=4)
    return (
        len(words) <= 4
        and any(word in _STAND_IN_MARKERS for word in words)
        and all(word in _STAND_IN_WORDS for word in words)
    )


def _is_exact_placeholder(value: str) -> bool:
    """A JWK value that is entirely a placeholder: empty, a placeholder word
    (``changeme``, ``REDACTED``) or shape (``<your-key>``, ``${JWK_D}``, ``****``).

    Unlike ``_is_placeholder``, a marker word inside an otherwise key-shaped
    value (``your...``, ``...dummy``) does not exempt it (PR #1147 review).
    """
    v = value.strip().strip("\"'")
    low = v.lower()
    return (
        not v
        or low in PLACEHOLDER_HINTS
        or low in _PLACEHOLDER_WORDS
        or bool(_PLACEHOLDER_SHAPE_RX.search(v))
        or bool(_EXACT_PLACEHOLDER_RX.fullmatch(v))
        or _is_marked_stand_in(v)
    )


# Shared wording, so `transcript_secret_finding` can restate a finding
# without re-matching free text.
IN_SOURCE_SUFFIX = " committed in source"
JWK_MEMBER_MESSAGE_PREFIX = "possible JWK private member "

# Files whose job is to hold secrets or machine-generated hashes.
SECRET_SCAN_SKIP = re.compile(r"(^|/)(\.env[^/]*|.*\.lock|package-lock\.json|.*\.min\.js)$")


def check_secrets(relpath: str, text: str):
    if SECRET_SCAN_SKIP.search(relpath):
        return
    found_lines = set()
    for i, line in enumerate(text.splitlines(), 1):
        for name, rx in (*SECRET_PATTERNS, *_SHARED_SECRET_SHAPES):
            m = rx.search(line)
            if not m:
                continue
            value = m.groupdict().get("val") or m.group(0)
            if _is_placeholder(value, m.group(0)):
                continue
            yield Finding(
                rule="SECRETS-KEY",
                severity="high",
                path=relpath,
                line=i,
                message=f"possible {name}{IN_SOURCE_SUFFIX}: {_mask(value)}",
                fix="move the credential to an environment variable or secret store, "
                "rotate it, and purge it from git history",
            )
            found_lines.add(i)
            break  # one finding per line is enough
    # JWK names are generic: inspect the complete object (including multiline
    # JSON), with ownership kept in this existing hardcoded-secret rule.
    from ..telemetry.jwk import audit_span, private_members

    members = private_members(text, coarse=False)
    if not members:
        return
    # Line numbers use the splitlines() model the loop above and the engine's
    # ua:allow suppression use (CR-only and Unicode separators included), from
    # offsets computed once (PR #1147 review: recounting from the start of the
    # file per member was quadratic).
    starts = list(itertools.accumulate((len(ln) for ln in text.splitlines(True)), initial=0))
    ranges = []
    for member in members:
        if _is_exact_placeholder(member.value):
            continue
        low, high = audit_span(member, text)
        first = bisect.bisect_right(starts, low)
        last = bisect.bisect_right(starts, max(low, high - 1))
        ranges.append((first, last, member.name))
    # Conservative decoded extents may overlap completely. Visit their union
    # once rather than repeating a line scan for every private member.
    covered = 0
    for first, last, name in sorted(ranges, key=lambda item: item[0]):
        for line_no in range(max(first, covered + 1), last + 1):
            if line_no in found_lines:
                continue
            found_lines.add(line_no)
            yield Finding(
                rule="SECRETS-KEY",
                severity="high",
                path=relpath,
                line=line_no,
                message=f'{JWK_MEMBER_MESSAGE_PREFIX}"{name}"{IN_SOURCE_SUFFIX}',
                fix="move the credential to an environment variable or secret store, "
                "rotate it, and purge it from git history",
            )
        covered = max(covered, last)


# Agent session transcripts and memories (task #2157). A harness writes every
# file an agent read into its session log, so redaction fixtures and test
# vectors land there by design. Such a file is local history, not source:
# "committed in source, purge it from git history" is wrong for it. The engine
# decides which files are transcripts (`engine._is_agent_transcript`) and
# passes their SECRETS-KEY findings through `transcript_secret_finding`:
#   - the message and fix say where the value really is;
#   - the JWK private-member heuristic, which matches generic member names in
#     any JSON-shaped text, drops to low. It stays visible but no longer fails
#     the gate on a fixture an agent happened to read;
#   - every vendor-shaped key pattern (sk-ant-, AKIA, gh*_ ...) and the
#     assigned-literal pattern stay high: a live key echoed into a transcript
#     is still a key on disk.
# Only SECRETS-KEY is touched. Credential stores, SECRETS-STORE-PERMS and
# every other rule run on these files exactly as on any other file.
TRANSCRIPT_SUFFIX = " in an agent session transcript, not source control"
TRANSCRIPT_JWK_SEVERITY = "low"


def transcript_secret_finding(finding: Finding) -> Finding:
    """Restate a SECRETS-KEY finding for a file in an agent session transcript."""
    if finding.rule != "SECRETS-KEY":
        return finding
    message = finding.message.replace(IN_SOURCE_SUFFIX, TRANSCRIPT_SUFFIX, 1)
    if message == finding.message:
        message = f"{finding.message}{TRANSCRIPT_SUFFIX}"
    jwk = finding.message.startswith(JWK_MEMBER_MESSAGE_PREFIX)
    return replace(
        finding,
        severity=TRANSCRIPT_JWK_SEVERITY if jwk else finding.severity,
        message=message,
        fix=(
            "if the value is a live credential, rotate it and delete or redact the "
            "transcript (local agent history: no git history to purge); a fixture or "
            "test vector the agent read needs no action"
        ),
    )


# Vendor-signed documents (task #2419). A client that manages a document writes
# a detached signature beside it: ``policy-limits.json`` and
# ``policy-limits.json.signature.json``. That file's whole job is to be the
# signature, so "a token committed in source" is the wrong reading of it:
# there is no credential to rotate and no history to purge. It was left only
# because the baseline is keyed by value hash, and the vendor mints a new
# signature every time it rewrites the policy — so the suppression stops
# matching and a security gate goes red on a file nobody can act on (#2363).
#
# The engine decides which files are signatures (`_signed_document`): a
# dot-directory outside every git work tree, a name ending in this suffix, the
# file it signs beside it, and content that is exactly one JWT (the check
# below). Only then is a SECRETS-KEY finding of the JSON-web-token shape
# restated here. The location is what keeps this from being a bypass: anyone
# can commit `foo.signature.json` beside an empty `foo`, so inside a repo the
# name and shape prove nothing and the file stays HIGH. Every other shape — a
# vendor key, a JWK private member, an assigned literal — keeps its severity
# in this file and everywhere else.
SIGNATURE_SUFFIX = ".signature.json"
JWT_SHAPE_NAME = "JSON web token"
JWT_IN_SOURCE_PREFIX = f"possible {JWT_SHAPE_NAME}{IN_SOURCE_SUFFIX}"
SIGNATURE_SUFFIX_TEXT = " in a detached signature file, not source control"
SIGNATURE_JWT_SEVERITY = "low"

_JWS_MIN_SIGNATURE_CHARS = 16
# A JWT cannot match this (no dots), so one cannot hide as an object key.
_SIGNATURE_KEY_RX = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _is_jws(token: str) -> bool:
    """A compact JWS: three base64url parts, the first decoding to a JOSE header."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return False
    if len(parts[2]) < _JWS_MIN_SIGNATURE_CHARS:
        return False
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    except (ValueError, binascii.Error):
        return False
    return isinstance(header, dict) and "alg" in header


def detached_signature(text: str) -> bool:
    """Whether ``text`` is one detached JWS and nothing else: a JSON string, or
    an object with exactly one string anywhere inside it, that string the token.

    Every level counts, and so do object keys, because ``check_secrets`` reports
    one JWT per line: a second token nested in a one-line document would be
    hidden behind the finding this file's first token gets restated as."""
    body = text.removeprefix("\ufeff").strip()
    if not body:
        return False
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return False
    if not isinstance(document, (str, dict)):
        return False
    strings: list[str] = []
    pending = [document]
    while pending:  # iterative: json.loads accepts nesting a recursive walk would not
        node = pending.pop()
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            if not all(_SIGNATURE_KEY_RX.fullmatch(key) for key in node):
                return False
            pending.extend(node.values())
    return len(strings) == 1 and _is_jws(strings[0])


def signature_secret_finding(finding: Finding) -> Finding:
    """Restate a SECRETS-KEY finding for a file that is a detached signature."""
    if finding.rule != "SECRETS-KEY" or not finding.message.startswith(JWT_IN_SOURCE_PREFIX):
        return finding
    return replace(
        finding,
        severity=SIGNATURE_JWT_SEVERITY,
        message=finding.message.replace(IN_SOURCE_SUFFIX, SIGNATURE_SUFFIX_TEXT, 1),
        fix=(
            "none needed when this file sits beside the document it signs and the "
            "signing tool owns both; a token anywhere else is still a credential"
        ),
    )


# Local credential stores (task #1846). Vendor CLIs keep their login state in
# a dot-directory under $HOME (`~/.codex/auth.json`, `~/.gemini/...-oauth-
# token`). Such a file is not source: "committed in source, purge it from git
# history" is wrong advice for it, and holding a token is its job. What can be
# wrong is its mode, so the engine swaps SECRETS-KEY for a permissions check on
# a file with one of these vendor basenames that sits under a dot-directory and
# is outside every git work tree or not tracked by git. Any other file, and a
# tracked one, keeps SECRETS-KEY: a stray key copy in a backup is still found.
VENDOR_CREDENTIAL_STORE_RX = re.compile(
    r"^(?:auth\.json|\.?credentials\.json|oauth_creds\.json|[^/]*oauth[-_]token"
    r"|\.codex-global-state\.json.*)$",
    re.IGNORECASE,
)


def check_credential_store_mode(relpath: str, mode: int, ancestor_modes: Iterable[int] = ()):
    """Report a local credential store that group or other users can read.

    Read bits alone are not exposure: a reader also needs search (x) on every
    directory above the file. ``ancestor_modes`` are those directories' modes.
    Group memberships are unknown here, so a directory blocks only when it
    grants neither g+x nor o+x (an owner-only 0700 parent or home).
    """
    if not mode & 0o044:
        return
    if not all(dir_mode & 0o011 for dir_mode in ancestor_modes):
        return
    perms = f"{mode & 0o777:04o}"
    yield Finding(
        rule="SECRETS-STORE-PERMS",
        severity="medium",
        path=relpath,
        message=f"credential store is group/world-readable (mode {perms})",
        fix=f"chmod 600 {relpath} (local tool state outside git: no history purge needed)",
    )


# The opening line of a private key, complete block or not. Shared with
# tools/public_relay.py, which must catch a truncated key in a draft too.
PRIVATE_KEY_HEADER_RX = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE)


def check_private_key(relpath: str, text: str):
    for i, line in enumerate(text.splitlines(), 1):
        if PRIVATE_KEY_HEADER_RX.search(line):
            yield Finding(
                rule="SECRETS-PRIVATE-KEY",
                severity="high",
                path=relpath,
                line=i,
                message="private key material found in the repository",
                fix="remove the key, rotate it, and store keys outside the repo "
                "(e.g. a keychain or secrets manager)",
            )
            return


# --- AI agent configuration -----------------------------------------------------

INSTRUCTION_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    ".github/copilot-instructions.md",
    "README.md",
)

# Invisible / direction-override characters usable to hide instructions from human
# reviewers while remaining visible to models. U+200D (ZWJ) is excluded: legitimate
# emoji sequences use it. A leading U+FEFF (BOM) is tolerated by the caller.
HIDDEN_CHAR_RX = re.compile(
    "[\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0001\U000e0020-\U000e007f]"
)

RISKY_INSTRUCTION_RX = re.compile(
    r"(?i)dangerously-skip-permissions|bypassPermissions|--yolo|--trust-all-tools|--no-verify"
)

BROAD_PERMISSIONS = {"*", "Bash", "Bash(*)", "Bash(*:*)", "Bash(**)"}


def _is_instruction_file(relpath: str) -> bool:
    return relpath in INSTRUCTION_FILES or relpath.startswith(".cursor/rules/")


def check_hidden_unicode(relpath: str, text: str):
    if not _is_instruction_file(relpath):
        return
    body = text.removeprefix("\ufeff")  # a leading BOM is benign
    for i, line in enumerate(body.splitlines(), 1):
        m = HIDDEN_CHAR_RX.search(line)
        if m:
            yield Finding(
                rule="AGENT-HIDDEN-UNICODE",
                severity="high",
                path=relpath,
                line=i,
                message=f"invisible/bidi Unicode (U+{ord(m.group(0)):04X}) in an agent "
                "instruction file — possible hidden prompt injection",
                fix="inspect the file with a hex viewer and delete the hidden characters; "
                "treat unexplained ones as hostile",
            )
            return


def check_risky_instructions(relpath: str, text: str):
    if not _is_instruction_file(relpath):
        return
    for i, line in enumerate(text.splitlines(), 1):
        m = RISKY_INSTRUCTION_RX.search(line)
        if m:
            yield Finding(
                rule="AGENT-RISKY-INSTRUCTIONS",
                severity="medium",
                path=relpath,
                line=i,
                message=f"instruction file tells agents to bypass a safety rail ({m.group(0)!r})",
                fix="remove the directive; grant narrow permissions in settings instead "
                "of disabling confirmation entirely",
            )


def _load_json(path: Path) -> dict | None:
    # Never path.read_text: a file that is, or becomes, a FIFO would block
    # the audit forever (#2212). Decoding is unchanged (locale, "replace").
    return _parse_json(read_regular_text(path, encoding=None, errors="replace"))


def _parse_json(text: str | None) -> dict | None:
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _hook_commands(node):
    """Yield every "command" string nested anywhere under a hooks config."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "command" and isinstance(v, str):
                yield v
            else:
                yield from _hook_commands(v)
    elif isinstance(node, list):
        for item in node:
            yield from _hook_commands(item)


# Host/workspace files that carry permissions or hook commands. Scanned at the
# audit root only (not a recursive hunt) so a random JSON in the tree is not
# treated as agent config.
AGENT_SETTINGS_PATHS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".agents/hooks.json",
    "hooks.json",
    "config/hooks.json",
)


def check_agent_settings(root: Path, files: list[Path], staged: dict[str, str] | None = None):
    """With `staged` (relpath -> text of a staged blob the working copy did
    not hold), judge those staged bytes instead of the working files."""
    for rel in AGENT_SETTINGS_PATHS:
        if staged is not None:
            if rel not in staged:
                continue
            data = _parse_json(staged[rel])
        else:
            p = root / rel
            if not p.is_file():
                continue
            data = _load_json(p)
        if data is None:
            yield Finding(
                rule="AGENT-PERMISSIVE-CONFIG",
                severity="info",
                path=rel,
                message="agent settings file is not valid JSON; could not audit it",
                fix="fix the JSON so permissions can be reviewed",
            )
            continue
        perms = data.get("permissions", {}) if isinstance(data.get("permissions"), dict) else {}
        if data.get("defaultMode") == "bypassPermissions" or perms.get("defaultMode") == (
            "bypassPermissions"
        ):
            yield Finding(
                rule="AGENT-PERMISSIVE-CONFIG",
                severity="high",
                path=rel,
                message="agent runs with bypassPermissions: every tool call is auto-approved",
                fix="use acceptEdits or an explicit allowlist instead of bypassing prompts",
            )
        for entry in perms.get("allow", []) if isinstance(perms.get("allow"), list) else []:
            if entry in BROAD_PERMISSIONS:
                yield Finding(
                    rule="AGENT-PERMISSIVE-CONFIG",
                    severity="high",
                    path=rel,
                    message=f"permission allowlist entry {entry!r} auto-approves arbitrary "
                    "commands",
                    fix="allow specific commands (e.g. 'Bash(npm test:*)') rather than "
                    "wildcards over Bash",
                )
        if rel.endswith("hooks.json"):
            nested = data.get("hooks")
            hook_root = nested if isinstance(nested, (dict, list)) else data
        else:
            hook_root = data.get("hooks")
        for cmd in _hook_commands(hook_root):
            if re.search(r"(?i)curl[^|;&]*\|\s*(?:ba)?sh|\beval\b", cmd):
                yield Finding(
                    rule="AGENT-HOOK-SHELL",
                    severity="medium",
                    path=rel,
                    message=f"hook pipes downloaded content to a shell or uses eval: {cmd[:80]!r}",
                    fix="vendor the script into the repo and execute it directly",
                )


ENV_REF_RX = re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")
# Host interpolators: Cursor `${env:VAR}`, OpenCode `{env:}` / `{file:}`.
ENV_INTERP_RX = re.compile(r"\$\{[A-Za-z_][^}]*\}|\{(?:env|file):[^}]+\}")
SECRETISH_VALUE_RX = re.compile(r"^[A-Za-z0-9+/=_\-]{16,}$")
BEARER_RX = re.compile(r"(?i)\bbearer\s+(\S{8,})")
# A base64-ish value that's actually just a filesystem path (e.g. env["UA_WORKSPACE"] in a
# satellite project's .mcp.json) — SECRETISH_VALUE_RX's charset includes '/' for base64, which
# also matches ordinary paths. Excludes '+' and '=' so genuine base64 secrets still get flagged.
PATH_LIKE_RX = re.compile(r"^/?(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+/?$")

MCP_CONFIG_PATHS = (
    ".mcp.json",
    ".cursor/mcp.json",
    "mcp.json",
    "config/mcp_config.json",
    ".gemini/config/mcp_config.json",
)


def _interpolated_secret(value: str) -> bool:
    return bool(ENV_REF_RX.search(value) or ENV_INTERP_RX.search(value))


def _literal_mcp_secret(value: str) -> str | None:
    """Return the token to mask, or None if this is not a committed secret."""
    if _interpolated_secret(value) or any(h in value.lower() for h in PLACEHOLDER_HINTS):
        return None
    bearer = BEARER_RX.search(value)
    if bearer:
        token = bearer.group(1)
        if _interpolated_secret(token):
            return None
        return token
    if SECRETISH_VALUE_RX.match(value) and not PATH_LIKE_RX.match(value):
        return value
    return None


_NETWORK_REMOTE_RX = re.compile(r"(?i)^(https?://|ssh://|git@|git://)")

# The git environment for each git call an audit run makes (#2225 review r4).
# `run_audit` installs a resolver for the run: the caller's binding, resolved
# by git from the caller's own directory with every path absolute, for the
# audited root's repository; a repository-neutral environment for any other.
# Outside a run (None) a git helper inherits the process environment.
AUDIT_GIT_ENV: ContextVar[Callable[[Path], dict | None] | None] = ContextVar(
    "audit_git_env", default=None
)


# The same binding with the caller's own git config left in place (#2225
# review r6). Only the local-only MCP downgrade reads it, and only to stay
# stricter: a global or transient `insteadOf`/`pushInsteadOf` that makes a raw
# local remote network-facing must keep the finding high.
AUDIT_GIT_CALLER_ENV: ContextVar[Callable[[Path], dict | None] | None] = ContextVar(
    "audit_git_caller_env", default=None
)


def audit_git_caller_env(directory: Path) -> dict | None:
    """The caller-effective environment for `directory` (see AUDIT_GIT_CALLER_ENV)."""
    resolve = AUDIT_GIT_CALLER_ENV.get()
    return None if resolve is None else resolve(directory)


def audit_git_env(directory: Path) -> dict | None:
    """The environment for a git call run in `directory` (see AUDIT_GIT_ENV)."""
    resolve = AUDIT_GIT_ENV.get()
    return None if resolve is None else resolve(directory)


def _git_file_tracked(root: Path, rel: str) -> bool | None:
    """Whether ``rel`` is tracked in ``root``'s git index.

    ``True`` when git lists it (exit 0) and ``False`` only when git positively
    says it is not in the index (exit 1). ``None`` means unknown: git is
    unavailable, timed out, the path is not in a repository, or git failed
    (exit 128 on a corrupt or unreadable index). Callers must treat ``None``
    as possibly tracked: ``False`` is what relaxes a finding (a credential
    store, an agent transcript, an untracked MCP config), so an unknown must
    never read as untracked (PR #1299 review).
    """
    env = audit_git_env(root)
    try:
        inside = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--error-unmatch", "--", rel],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if res.returncode == 0:
            return True
        if res.returncode == 1:
            return False
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_ignored_paths(cwd: Path, rels: list[str]) -> set[str]:
    """The subset of ``rels`` (relative to ``cwd``) that git ignores.

    fsmonitor is off in every audit git call, so an audit never starts a
    monitor program a repository configures (#2225).

    One ``git check-ignore --stdin`` call; tracked files are never reported.
    Empty when git is unavailable or ``cwd`` is not in a work tree. Names
    travel as bytes (``os.fsencode``), so a name that is not valid UTF-8 is
    checked rather than crashing the audit. The git environment comes from
    `audit_git_env`, like every audit git call.
    """
    if not rels:
        return set()
    try:
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "check-ignore", "--stdin", "-z"],
            cwd=cwd,
            input=b"".join(os.fsencode(rel) + b"\0" for rel in rels),
            capture_output=True,
            timeout=10,
            check=False,
            env=audit_git_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if res.returncode not in (0, 1):  # 1: nothing ignored; 128: not a repo
        return set()
    return {os.fsdecode(p) for p in res.stdout.split(b"\0") if p}


def _git_has_network_remote(root: Path) -> bool | None:
    """``True`` if any remote URL is network-facing; ``False`` if only local paths.

    Local bare remotes (`/path/to.git`, ``file://…``) cannot leak a secret to
    GitHub — the operator treats those hub bearer tokens as identification, not a
    high finding. ``None`` when remotes cannot be enumerated.

    During an audit run the remotes are read twice, through the isolated
    environment and through the caller's own (#2225 review r6). ``False``, the
    only answer that downgrades, needs both views to be local-only. Network
    in either view is ``True``; anything else is ``None``.
    """
    isolated = _remote_view(root, audit_git_env(root))
    if AUDIT_GIT_CALLER_ENV.get() is None:
        return isolated
    caller = _remote_view(root, audit_git_caller_env(root))
    if isolated is True or caller is True:
        return True
    if isolated is False and caller is False:
        return False
    return None


def _remote_view(root: Path, env: dict | None) -> bool | None:
    """Classify the remotes `git remote -v` reports under `env`. Fetch URLs
    there have `insteadOf` applied and push URLs `pushInsteadOf`, as git
    applies them."""
    try:
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "remote", "-v"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if res.returncode != 0:
            return None
        urls: list[str] = []
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            urls.append(parts[1])
        if not urls:
            # No remotes yet — keep high (a secret may still be pushed once
            # origin is added). Only explicit local-path remotes downgrade.
            return None
        for url in urls:
            if _NETWORK_REMOTE_RX.match(url):
                return True
            # file:// is local; absolute / relative paths are local.
            if url.startswith("file://") or url.startswith("/") or url.startswith("."):
                continue
            # Anything else (scp-like host:path without git@) still network-ish.
            if ":" in url and not url.startswith("/"):
                return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return None


def _holding_directory(root: Path, rel: str) -> tuple[Path, str]:
    """The deepest real directory on `rel`'s path below `root`, and `rel`
    relative to it. Git run there finds the innermost repository holding the
    file. The descent stops at a symlink, which git does not follow either,
    and at a directory that no longer exists (a staged, deleted file)."""
    parts = PurePosixPath(rel).parts
    directory = root
    depth = 0
    for part in parts[:-1]:
        child = directory / part
        if child.is_symlink() or not child.is_dir():
            break
        directory = child
        depth += 1
    return directory, "/".join(parts[depth:])


def _mcp_secret_severity(root: Path, rel: str) -> str | None:
    """Severity for AGENT-MCP-SECRETS, or ``None`` to skip the finding.

    Untracked MCP configs are local host state — skip. Tracked configs whose
    remotes are all local paths downgrade high → info. Network remotes (or
    unknown git state) keep high.

    The file is judged by the repository that holds it, which need not be
    `root`'s: a deeper repository can sit at one of `root`'s config paths, and
    `root`'s index calls that file untracked (#2263).
    """
    directory, rel = _holding_directory(root, rel)
    tracked = _git_file_tracked(directory, rel)
    if tracked is False:
        return None
    if tracked is True:
        network = _git_has_network_remote(directory)
        if network is False:
            return "info"
    return "high"


def _check_one_mcp_config(rel: str, data: dict, *, secret_severity: str = "high"):
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        env = server.get("env") if isinstance(server.get("env"), dict) else {}
        for key, value in env.items():
            if not isinstance(value, str):
                continue
            token = _literal_mcp_secret(value)
            if token:
                yield Finding(
                    rule="AGENT-MCP-SECRETS",
                    severity=secret_severity,
                    path=rel,
                    message=f"MCP server {name!r} has a literal secret in env[{key!r}]: "
                    f"{_mask(token)}",
                    fix='reference an environment variable (e.g. "${API_KEY}") instead of '
                    "committing the value",
                )
        headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
        for key, value in headers.items():
            if not isinstance(value, str):
                continue
            token = _literal_mcp_secret(value)
            if token:
                yield Finding(
                    rule="AGENT-MCP-SECRETS",
                    severity=secret_severity,
                    path=rel,
                    message=f"MCP server {name!r} has a literal secret in headers[{key!r}]: "
                    f"{_mask(token)}",
                    fix="use a host env/file interpolator (e.g. ${env:UA_CURSOR_HUB_TOKEN}) "
                    "instead of committing the value",
                )
        argv = [server.get("command", "")] + (
            server.get("args", []) if isinstance(server.get("args"), list) else []
        )
        runner = next((a for a in argv if a in ("npx", "uvx")), None)
        if runner:
            pkg = next(
                (a for a in argv[argv.index(runner) + 1 :] if a and not a.startswith("-")),
                None,
            )
            if pkg and "@" not in pkg.lstrip("@"):
                yield Finding(
                    rule="AGENT-MCP-UNPINNED",
                    severity="medium",
                    path=rel,
                    message=f"MCP server {name!r} runs {runner} {pkg!r} without a version pin "
                    "(supply-chain risk: you execute whatever is published next)",
                    fix=f"pin an exact version, e.g. {pkg}@1.2.3, and review before bumping",
                )


def check_mcp_config(root: Path, files: list[Path], staged: dict[str, str] | None = None):
    """With `staged`, as in `check_agent_settings`: the staged blob's bytes
    are judged, so a cleaned or deleted working copy cannot hide them."""
    seen: set[Path] = set()
    for rel in MCP_CONFIG_PATHS:
        if staged is not None:
            if rel not in staged:
                continue
            data = _parse_json(staged[rel])
        else:
            p = (root / rel).resolve()
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            data = _load_json(p)
        if not isinstance(data, dict):
            continue
        severity = _mcp_secret_severity(root, rel)
        if severity is None:
            # Still flag unpinned packages on untracked configs — supply-chain
            # risk is independent of whether the file is committed.
            yield from (
                fd
                for fd in _check_one_mcp_config(rel, data, secret_severity="high")
                if fd.rule != "AGENT-MCP-SECRETS"
            )
            continue
        yield from _check_one_mcp_config(rel, data, secret_severity=severity)


def check_instructions_exist(root: Path, files: list[Path]):
    if any((root / f).is_file() for f in INSTRUCTION_FILES[:-1]):  # README alone ≠ guardrails
        return
    yield Finding(
        rule="AGENT-NO-INSTRUCTIONS",
        severity="info",
        message="no agent instruction file (CLAUDE.md / AGENTS.md / .cursorrules) found",
        fix="add one stating build/test commands and guardrails (what agents must not touch), "
        "so every assistant starts with the same constraints",
    )


# --- LLM application code ---------------------------------------------------------

_PY_LLM_MODULES = (
    r"(?:anthropic|openai|google\.generativeai|google\.genai"
    r"|litellm|langchain\w*|mistralai|cohere|groq|ollama)"
)
# Regex fallback only for sources that do not parse; check_py_ast reads the
# Import/ImportFrom nodes so `import json, anthropic` is seen too.
PY_LLM_IMPORT_RX = re.compile(rf"(?m)^\s*(?:from|import)\s+{_PY_LLM_MODULES}\b")
_PY_LLM_MODULE_RX = re.compile(rf"{_PY_LLM_MODULES}(?:\.|$)")
_PY_SUBPROCESS_FUNCS = ("run", "Popen", "call", "check_call", "check_output")
# Calls that always hand their command string to a shell.
_PY_IMPLICIT_SHELL = {
    "os.system",
    "os.popen",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "asyncio.create_subprocess_shell",
}
JS_LLM_IMPORT_RX = re.compile(
    r"""["'](?:@anthropic-ai/sdk|openai|@google/generative-ai|ai|langchain)["']"""
)


def _py_llm_regex_scan(relpath: str, text: str):
    """Line-regex fallback for Python sources that don't parse; parseable
    files get the sharper AST checks in check_py_ast instead."""
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"\b(?:eval|exec)\(", line):
            yield Finding(
                rule="LLM-EVAL",
                severity="high",
                path=relpath,
                line=i,
                message="eval/exec in a file that talks to an LLM — model output may "
                "reach code execution",
                fix="parse structured output (JSON/tool calls) instead of evaluating it; "
                "if execution is the feature, sandbox it",
            )
        if re.search(r"shell\s*=\s*True", line):
            yield Finding(
                rule="LLM-SHELL-TRUE",
                severity="medium",
                path=relpath,
                line=i,
                message="subprocess(shell=True) in a file that talks to an LLM — "
                "model-influenced strings may reach a shell",
                fix="pass an argv list with shell=False and validate against an allowlist",
            )


def check_llm_code(relpath: str, text: str):
    # Python files are covered by check_py_ast (AST checks with a regex
    # fallback for unparseable sources); running both double-reported
    # every eval/exec and shell=True finding.
    is_js = relpath.endswith((".js", ".ts", ".mjs", ".tsx", ".jsx"))
    if is_js and JS_LLM_IMPORT_RX.search(text):
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\beval\(|new\s+Function\(", line):
                yield Finding(
                    rule="LLM-EVAL",
                    severity="high",
                    path=relpath,
                    line=i,
                    message="eval/new Function in a file that talks to an LLM — model output "
                    "may reach code execution",
                    fix="parse structured output instead of evaluating it",
                )


def _py_import_aliases(tree):
    """Index call-site lexical scopes and imports, not runtime data flow.

    A local binding blocks inherited aliases. Imports in that same scope remain
    conservative candidates regardless of statement order or conditional branches.
    Scope maps are shared by calls; never copy a file's aliases for every node.
    """
    import ast

    class Imports(ast.NodeVisitor):
        def __init__(self):
            self.scope = ChainMap()
            self.closure = self.scope
            self.calls = {}
            self.has_llm = False

        def visit_Call(self, node):
            self.calls[node] = self.scope
            self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.scope.maps[0].setdefault(node.id, None)

        def visit_arg(self, node):
            self.scope.maps[0].setdefault(node.arg, None)

        def visit_Import(self, node):
            for a in node.names:
                self.has_llm |= bool(_PY_LLM_MODULE_RX.match(a.name))
                self.scope[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )

        def visit_ImportFrom(self, node):
            for a in node.names:
                target = f"{node.module}.{a.name}"
                self.has_llm |= not node.level and bool(_PY_LLM_MODULE_RX.match(target))
                self.scope[a.asname or a.name] = target if not node.level else None

        def visit_scope(self, node):
            if hasattr(node, "name"):
                self.scope.maps[0].setdefault(node.name, None)
            # Defaults, decorators, bases and annotations execute outside the body.
            for field, value in ast.iter_fields(node):
                if field not in ("body", "args"):
                    for child in value if isinstance(value, list) else [value]:
                        if isinstance(child, ast.AST):
                            self.visit(child)
            args = getattr(node, "args", None)
            parameters = []
            if args is not None:
                parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                parameters.extend(arg for arg in (args.vararg, args.kwarg) if arg is not None)
                for arg in parameters:
                    if arg.annotation is not None:
                        self.visit(arg.annotation)
                for default in (*args.defaults, *args.kw_defaults):
                    if default is not None:
                        self.visit(default)
            outer, closure = self.scope, self.closure
            self.scope = closure.new_child()
            # Class namespaces are visible in the class body, not in methods.
            if not isinstance(node, ast.ClassDef):
                self.closure = self.scope
            for arg in parameters:
                self.visit_arg(arg)
            for child in node.body if isinstance(node.body, list) else [node.body]:
                self.visit(child)
            self.scope, self.closure = outer, closure

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = visit_scope

        def visit_comprehension_scope(self, node):
            # The first iterable is evaluated in the enclosing scope.
            self.visit(node.generators[0].iter)
            outer, closure = self.scope, self.closure
            self.scope = self.closure = closure.new_child()
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                self.visit(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            for field in ("elt", "key", "value"):
                if hasattr(node, field):
                    self.visit(getattr(node, field))
            self.scope, self.closure = outer, closure

        visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = (
            visit_comprehension_scope
        )

    imports = Imports()
    imports.visit(tree)
    return imports.calls, imports.has_llm


def _py_call_name(func, aliases: Mapping[str, str | None]) -> str | None:
    """Dotted name of a call target with import aliases resolved."""
    import ast

    if isinstance(func, ast.Name):
        return aliases.get(func.id) or func.id
    if isinstance(func, ast.Attribute):
        base = _py_call_name(func.value, aliases)
        return f"{base}.{func.attr}" if base else None
    return None


def _py_shell_truthy(node) -> bool:
    """shell=<truthy constant>, directly or through a literal **{'shell': ...}."""
    import ast

    for kw in node.keywords:
        if kw.arg == "shell":
            val = kw.value
        elif kw.arg is None and isinstance(kw.value, ast.Dict):
            val = next(
                (
                    v
                    # Dict displays keep the last value for a duplicate key.
                    for k, v in zip(reversed(kw.value.keys), reversed(kw.value.values), strict=True)
                    if isinstance(k, ast.Constant) and k.value == "shell"
                ),
                None,
            )
        else:
            continue
        if isinstance(val, ast.Constant) and val.value:
            return True
    return False


def check_py_ast(relpath: str, text: str):
    if not relpath.endswith(".py"):
        return
    import ast

    try:
        tree = ast.parse(text)
        call_aliases, has_llm = _py_import_aliases(tree)
    except (SyntaxError, RecursionError):
        if PY_LLM_IMPORT_RX.search(text):
            yield from _py_llm_regex_scan(relpath, text)
        return

    if not has_llm:
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        bare = func.id if isinstance(func, ast.Name) else None
        name = _py_call_name(func, call_aliases[node]) or ""
        # Keep legacy syntactic detections even when a binding is ambiguous.
        syntactic = _py_call_name(func, {}) or ""
        short = name.rsplit(".", 1)[-1]

        # 1. Detect eval / exec (bare names or resolved through builtins)
        if bare in ("eval", "exec") or name in ("builtins.eval", "builtins.exec"):
            yield Finding(
                rule="LLM-EVAL",
                severity="high",
                path=relpath,
                line=node.lineno,
                message=f"use of built-in {short}() function detected — dangerous code execution vulnerability",
                fix=f"avoid calling {short}(); parse structured data (like json.loads) instead",
            )

        # 2. Detect shell execution
        if name not in _PY_IMPLICIT_SHELL and syntactic in _PY_IMPLICIT_SHELL:
            name = syntactic
        if bare in ("system", "popen") and name not in _PY_IMPLICIT_SHELL:
            name = f"os.{bare}"  # unchanged legacy reading of a bare system()/popen()
        if name in _PY_IMPLICIT_SHELL:
            yield Finding(
                rule="LLM-SHELL-TRUE",
                severity="medium",
                path=relpath,
                line=node.lineno,
                message=f"use of {name}() detected which implicitly executes in a shell",
                fix="use subprocess.run with shell=False and pass arguments as a list",
            )

        is_subproc = bare in _PY_SUBPROCESS_FUNCS or bool(
            {name, syntactic} & {f"subprocess.{f}" for f in _PY_SUBPROCESS_FUNCS}
        )
        if is_subproc and _py_shell_truthy(node):
            yield Finding(
                rule="LLM-SHELL-TRUE",
                severity="medium",
                path=relpath,
                line=node.lineno,
                message="subprocess call with shell=True detected",
                fix="pass command arguments as a list and set shell=False",
            )

        # Hardcoded secrets are covered by check_secrets, which scans every
        # line with SECRET_PATTERNS regardless of assignment shape; an
        # AST-level check here double-reported the same values.


# --- repo hygiene ------------------------------------------------------------------

GITIGNORE_ENV_PATTERNS = {".env", ".env*", "*.env", ".env.*", "**/.env"}


def _gitignore_covers_env(gitignore: Path) -> bool:
    """True if this single .gitignore has a line matching a basename env pattern.

    Only literal `.env` / `.env*`-style basename patterns are recognised — full
    gitignore semantics (negations, dir-anchoring) are intentionally out of scope.
    """
    # Never is_file() then read_text: a .gitignore swapped for a FIFO in
    # between would block the audit forever (#2212).
    text = read_regular_text(gitignore, encoding=None, errors="replace")
    if text is None:
        return False
    lines = {ln.strip() for ln in text.splitlines()}
    return bool(lines & GITIGNORE_ENV_PATTERNS)


def _env_covered_in_chain(env_file: Path, root: Path) -> bool:
    """Walk the nearest-ancestor .gitignore chain from the .env's directory up to
    (and including) the scanned root; a covering pattern at any level ignores it.

    Git applies a parent directory's .gitignore to nested files, so an ancestor
    `.env*` rule genuinely covers a nested .env. The previous implementation only
    read `<root>/.gitignore`, which false-flagged any nested project that shipped
    its own correct .gitignore (e.g. projects/imessage-bridge/.gitignore).
    """
    d = env_file.parent
    while True:
        if _gitignore_covers_env(d / ".gitignore"):
            return True
        if d == root:
            return False
        d = d.parent


def _repo_in_chain(env_file: Path, root: Path) -> bool:
    """True if any directory from the .env's dir up to the scanned root carries a
    `.git` marker — a directory for a normal repo, or a file for a worktree/submodule.
    A repo whose root sits *above* the scanned root is out of scope by design.
    """
    d = env_file.parent
    while True:
        if (d / ".git").exists():
            return True
        if d == root:
            return False
        d = d.parent


def check_env_exposure(root: Path, files: list[Path]):
    env_files = [f for f in files if f.name == ".env" or f.name.startswith(".env.")]
    for env_file in env_files:
        if _env_covered_in_chain(env_file, root):
            continue
        # Severity turns on whether this file could actually reach a commit.
        #   - A `.git` marker anywhere in the chain → the .env is one `git add`
        #     away from a live secret leak → high.
        #   - No repo in scope AND the .env sits directly at the scanned root →
        #     still high: auditors point the tool at project roots, so a top-level
        #     .env signals intent-to-version-control even before `git init` (and
        #     the scan boundary may itself sit inside a larger repo we can't see).
        #   - No repo in scope for a NESTED .env → downgrade high → info: with
        #     nothing to `git add` it into, the exposure is informational rather
        #     than a live leak. We surface it (a repo may be initialised later)
        #     instead of suppressing it outright.
        at_scan_root = env_file.parent == root
        severity = "high" if (_repo_in_chain(env_file, root) or at_scan_root) else "info"
        rel = str(env_file.relative_to(root))
        yield Finding(
            rule="SECRETS-ENV-EXPOSED",
            severity=severity,
            path=rel,
            message=".env file present but no .gitignore rule covers it — secrets are one "
            "`git add .` away from being committed (and readable by any agent/tool)",
            fix="add `.env*` to .gitignore; if already committed, rotate the secrets",
        )


def check_lockfiles(root: Path, files: list[Path]):
    if (root / "package.json").is_file() and not any(
        (root / lf).is_file()
        for lf in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")
    ):
        yield Finding(
            rule="HYGIENE-NO-LOCKFILE",
            severity="low",
            path="package.json",
            message="no JS lockfile — installs are unpinned (agents may add hallucinated or "
            "typosquatted packages unnoticed)",
            fix="commit a lockfile and review dependency diffs in PRs",
        )
    if (root / "pyproject.toml").is_file() and not any(
        (root / lf).is_file() for lf in ("uv.lock", "poetry.lock", "pdm.lock")
    ):
        yield Finding(
            rule="HYGIENE-NO-LOCKFILE",
            severity="low",
            path="pyproject.toml",
            message="no Python lockfile — installs are unpinned",
            fix="commit uv.lock / poetry.lock so environments are reproducible",
        )


def check_gitignore_exists(root: Path, files: list[Path]):
    if (root / ".git").exists() and not (root / ".gitignore").is_file():
        yield Finding(
            rule="HYGIENE-NO-GITIGNORE",
            severity="low",
            message="git repository without a .gitignore",
            fix="add one covering .env*, credentials, and build artifacts before agents "
            "start committing",
        )


CONTENT_RULES = [
    check_secrets,
    check_private_key,
    check_hidden_unicode,
    check_risky_instructions,
    check_llm_code,
    check_py_ast,
]


# The injection phrase patterns (_EXTRACT_VERBS, INJECTION_KEYWORDS, INJECTION_RX)
# live in the kernel module injection_patterns (#2450), imported above.

_BASE64_MIN_RUN = 64
BASE64_BLOB_RX = re.compile(rf"\b[A-Za-z0-9+/]{{{_BASE64_MIN_RUN},}}=*\b")

# Hex is a 16-character alphabet ([0-9a-fA-F]), a strict subset of base64's
# 64-character alphabet. A pure-hex blob is not a base64 payload: the first
# byte of every decoded 3-byte group takes its top six bits from one hex
# character (base64 value 0-5, 26-31 or 52-61), so the only printable ASCII it
# can produce there is 0x68-0x7E ('h'..'~') — no spaces, no capitals, no
# 'a'..'g'. Base64-decoding hex cannot spell a directive. See task #463.
#
# What hex CAN carry is a hex-encoded directive (`bytes.hex()` of plain text).
# The exemption used to key on a `sha|hash|digest` word on the same line, which
# both missed real digests (a wrapped YAML value, "tracked patch `9ee69c…`" —
# task #1372) and let a labelled hex-encoded payload through. It now keys on
# content: a digest-length hex blob is a digest when hex-decoding it yields
# binary noise rather than text. 32 random bytes are ~37% printable on average;
# text is ~100%. The threshold sits far from both.
_HEX_CHARSET = frozenset("0123456789abcdefABCDEF")
_HEX_DIGEST_LENGTHS = frozenset({32, 40, 64, 96, 128})
_TEXT_BYTES = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
_TEXT_RATIO = 0.75

# Digits and '/' only: task-id lists like `976/977/994/…` (#1372). Their base64
# values are 52-61 and 63, so every decoded group opens with a byte >= 0xD0 —
# never ASCII — and '/' rules out hex. Such a run cannot carry text either way.
_ID_LIST_CHARSET = frozenset("0123456789/")


def _decodes_to_text(raw: bytes) -> bool:
    """True when bytes look like text. NULs are dropped first so UTF-16 text
    (every other byte NUL) is still recognized as text. Non-ASCII text is
    caught by a strict UTF-8 decode: 32 random bytes essentially never form
    valid UTF-8 (0 in 10^6 trials), while encoded prose in any script does.
    The decoder runs non-final so a sequence cut off at the blob's end still
    counts."""
    body = raw.replace(b"\x00", b"")
    if not body:
        return False
    if sum(b in _TEXT_BYTES for b in body) / len(body) >= _TEXT_RATIO:
        return True
    try:
        chars = codecs.getincrementaldecoder("utf-8")("strict").decode(body, final=False)
    except UnicodeDecodeError:
        return False
    return (
        bool(chars)
        and sum(c.isprintable() or c.isspace() for c in chars) / len(chars) >= _TEXT_RATIO
    )


# Compressed bytes are indistinguishable from a random digest by content, so a
# hex-encoded compressed directive would pass the text test (Devin review on
# #768). A blob opening with a compression/container magic is not treated as a
# digest. Random digests hit one of these prefixes ~4/65536 of the time (zlib's
# 0x78 pairs dominate), and those simply keep firing as they did pre-#1372.
_CONTAINER_MAGIC = (
    b"\x78\x01",  # zlib, no/low compression
    b"\x78\x5e",  # zlib, fast
    b"\x78\x9c",  # zlib, default
    b"\x78\xda",  # zlib, best
    b"\x1f\x8b",  # gzip
    b"\x28\xb5\x2f\xfd",  # zstd
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"\x04\x22\x4d\x18",  # lz4 frame
)
# Bounded inflate output: a 64-byte blob cannot legitimately need more, and the
# cap keeps a crafted stream from ballooning.
_INFLATE_CAP = 4096


def _inflates_to_text(raw: bytes) -> bool:
    """Headerless raw deflate carries no magic, so try a bounded inflate (raw,
    zlib and gzip framings) and apply the same text test to whatever comes out.
    Truncated streams still yield their partial output."""
    for wbits in (-15, 15, 31):
        try:
            out = zlib.decompressobj(wbits).decompress(raw, _INFLATE_CAP)
        except zlib.error:
            continue
        if len(out) >= 8 and _decodes_to_text(out):
            return True
    return False


def _is_hex_digest(blob: str) -> bool:
    """A conventional-length hex digest: hex-only, and neither hex-encoded
    text nor a hex-encoded compressed stream."""
    if len(blob) not in _HEX_DIGEST_LENGTHS or not set(blob) <= _HEX_CHARSET:
        return False
    raw = bytes.fromhex(blob)
    if raw.startswith(_CONTAINER_MAGIC):
        return False
    return not (_decodes_to_text(raw) or _inflates_to_text(raw))


# One path segment spelled as words: lowercase or camel/PascalCase runs, each
# word optionally ending in digits (`lib64`, `getConnection`, `Http2Server`).
# Random base64 almost never produces a segment like this — capitals are
# followed by digits or more capitals as often as by lowercase — and never
# three or more in a row (see test_random_base64_is_never_path_like).
_PATH_SEGMENT_RX = re.compile(r"[a-z]+[0-9]*(?:[A-Z][a-z]+[0-9]*)*|(?:[A-Z][a-z]+[0-9]*)+")


def _is_source_path(blob: str) -> bool:
    """A `/`-separated run of word-like segments is a file path (task #1386).

    Source citations such as `packages/server/lib/controllers/connection/
    connectionId/getConnection` sit wholly inside the base64 alphabet. They
    are exempt only when every segment reads as a word AND the run does not
    base64-decode to text, so a word-shaped string that decodes to a directive
    is still reported.
    """
    if "+" in blob or "=" in blob:
        return False
    segments = blob.split("/")
    if len(segments) < 3 or not all(_PATH_SEGMENT_RX.fullmatch(seg) for seg in segments):
        return False
    # Decode the longest whole-quantum prefix: every run in this alphabet
    # decodes, so the text check always runs (a padding error must not count
    # as "safe").
    whole = blob[: len(blob) - len(blob) % 4]
    try:
        raw = base64.b64decode(whole, validate=True)
    except (binascii.Error, ValueError):
        return False
    return not _decodes_to_text(raw)


# Share of lowercase letters (ignoring '/') at or above which a run reads as
# words rather than encoded bytes (task #1631). Base64 of random, compressed
# or encrypted bytes spreads evenly over 64 symbols, so about 41% of it is
# lowercase: the chance a 64-character random run reaches 80% is ~1e-10 (see
# test_random_base64_is_never_a_lowercase_word_run). Words do: URL paths after
# a host (`com/HarnessRouter/harnessrouter/blob/main/...`), slash-joined API
# names (`waitForURL/waitForLoadState/reload`) and prose with its spaces
# stripped all measure 0.86-0.93. Plain text base64-encodes to ~45% lowercase,
# and the decode check below catches any that is not.
_WORD_RUN_LOWER_RATIO = 0.8
_WORD_RUN_MIN_DISTINCT = 8
# An encoded payload followed by enough lowercase prose can average above the
# word ratio, and the whole-run decode then buries it in the prose's garbage
# bytes (Devin review, PR #928). So a decode also counts as a payload when it
# contains a printable ASCII stretch this long anywhere: a directive is
# readable whatever follows it, while decoded prose yields such a stretch with
# probability about 0.37**18, roughly 1e-8 per position.
_TEXT_RUN_MIN = 18
_TEXT_RUN_RX = re.compile(rb"[\x20-\x7e\t\r\n]{%d,}" % _TEXT_RUN_MIN)


def _payload_at_any_alignment(blob: str) -> bool:
    """True when the run, read from any of its four offsets, base64-decodes to
    text, a container magic, or a deflate stream that inflates to text.

    A run is matched from wherever the regex starts it (often just after a
    '.' or a space), so the encoded bytes may begin 1-3 characters in; a
    payload behind `evil.io/` would be misread at offset 0 alone.
    """
    for offset in range(4):
        chunk = blob[offset:]
        chunk = chunk[: len(chunk) - len(chunk) % 4]
        if len(chunk) < 8:
            continue
        try:
            raw = base64.b64decode(chunk, validate=True)
        except (binascii.Error, ValueError):
            return True  # cannot vouch for what it decodes to
        if (
            raw.startswith(_CONTAINER_MAGIC)
            or _decodes_to_text(raw)
            or _TEXT_RUN_RX.search(raw)
            or _inflates_to_text(raw)
        ):
            return True
    return False


# The average ratio can be bought with padding: a short encoded payload glued
# to enough lowercase prose still averages above 0.8, and a payload under
# _TEXT_RUN_MIN bytes, one broken by control bytes, or raw deflate placed after
# the prose all evade the decode checks (independent review of PR #928). The
# encoded stretch itself stays ~45% lowercase wherever it sits, so every
# window of this many characters must also read as words: at least this share
# lowercase. A 15-byte payload encodes to 20 characters, so one window lands on
# it whole.
#
# Neutral runs carry no case signal and are dropped before windowing rather
# than counted as words: digit runs (dates, timestamps, ids; Devin on #928)
# and hex runs that include a digit (a commit SHA in a URL path). Dropping, not
# crediting, means a neutral filler spliced between payload chunks cannot lift
# the chunks' windows, and an a-f-only run is plain lowercase like any prose.
# Residual, accepted: a payload cut into pieces of ~9 bytes or less, each
# separated by real lowercase filler, still passes; its decode is a few bytes
# of text between runs of junk, which the decode checks cannot see either.
_WORD_RUN_WINDOW = 20
_WORD_RUN_WINDOW_MIN_LOWER = 0.6
_HEX_RUN_RX = re.compile(r"[0-9a-f]+")
_DIGIT_RUN_RX = re.compile(r"[0-9]{4,}")


def _strip_neutral_runs(body: str) -> str:
    """Drop digit runs of 4+ and hex runs of 12+ that contain a digit.

    One linear pass over maximal [0-9a-f] runs (a lookahead regex went
    quadratic on long all-letter runs; Devin on #928). Digit runs always sit
    inside a hex run, so each hex run is either dropped whole or has only its
    long digit stretches removed.
    """
    out = []
    last = 0
    for m in _HEX_RUN_RX.finditer(body):
        run = m.group()
        if not any(c.isdigit() for c in run):
            continue
        out.append(body[last : m.start()])
        if len(run) < 12:  # a SHA-like run of 12+ is dropped whole
            out.append(_DIGIT_RUN_RX.sub("", run))
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def _every_window_word_like(body: str) -> bool:
    """True when each _WORD_RUN_WINDOW-character window of `body`, with its
    neutral digit and hex runs removed, is mostly lowercase."""
    letters = _strip_neutral_runs(body)
    if not letters:
        return True
    size = min(len(letters), _WORD_RUN_WINDOW)
    need = _WORD_RUN_WINDOW_MIN_LOWER * size
    lower = [c.islower() for c in letters]
    count = sum(lower[:size])
    if count < need:
        return False
    for i in range(size, len(letters)):
        count += lower[i] - lower[i - size]
        if count < need:
            return False
    return True


def _is_lowercase_word_run(blob: str) -> bool:
    """A run spelled mostly in lowercase letters is words, not encoded bytes.

    Covers URL paths, slash-joined identifiers and space-stripped prose
    (task #1631). Exempt only when no alignment decodes to a payload, so a
    lowercase-heavy run that does decode to text still reports.
    """
    if "+" in blob or "=" in blob:
        return False
    body = blob.replace("/", "")
    # Words use many letters; a run of one repeated symbol (`zzzz...`) is not
    # prose and keeps reporting.
    if len(set(body)) < _WORD_RUN_MIN_DISTINCT:
        return False
    if sum(c.islower() for c in body) / len(body) < _WORD_RUN_LOWER_RATIO:
        return False
    if not _every_window_word_like(body):
        return False
    return not _payload_at_any_alignment(blob)


def _is_benign_without_digests(blob: str) -> bool:
    """A run made benign by dropping its hex-digest path segments (task #1846).

    A commit SHA in a GitHub blob/commit URL (`repo/blob/<40 hex>/docs/...`)
    sinks the lowercase-word ratio with its digits, so the whole run used to
    report. A segment that `_is_hex_digest` accepts is noise, not a payload;
    what remains must not decode to a payload, and is then judged on its own
    (too short to be a blob at all when under the rule's minimum run).
    """
    segments = [seg for seg in blob.split("/") if seg]
    kept = [seg for seg in segments if not _is_hex_digest(seg)]
    if len(kept) == len(segments):
        return False
    rest = "/".join(kept)
    # Short or not, the remainder must not decode to anything: a digest next
    # to a small encoded instruction must not exempt it (PR #1000 review).
    if not _is_inert_url_part(rest):
        return False
    return len(rest) < _BASE64_MIN_RUN or _is_benign_base64_shape(rest)


def _is_benign_base64_shape(blob: str) -> bool:
    """Matches of BASE64_BLOB_RX that structurally cannot hold a payload."""
    return (
        set(blob) <= _ID_LIST_CHARSET
        or _is_hex_digest(blob)
        or _is_source_path(blob)
        or _is_lowercase_word_run(blob)
        or _is_benign_without_digests(blob)
    )


# A URL in a document: scheme, then everything up to whitespace or a quote.
_URL_RX = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"'`]+", re.IGNORECASE)
_HEX_TEXT_RUN_RX = re.compile(r"[0-9a-fA-F]{32,}")


def _hex_carries_payload(blob: str) -> bool:
    """True when a long hex run inside `blob` hex-decodes (at either nibble
    alignment) to text, a container magic, or a deflate stream of text."""
    for m in _HEX_TEXT_RUN_RX.finditer(blob):
        run = m.group()
        for offset in (0, 1):
            chunk = run[offset:]
            raw = bytes.fromhex(chunk[: len(chunk) - len(chunk) % 2])
            if raw.startswith(_CONTAINER_MAGIC) or _decodes_to_text(raw) or _inflates_to_text(raw):
                return True
    return False


def _is_inert_url_part(blob: str) -> bool:
    """A run inside a URL is an address (path, id, SHA), not a document
    payload, unless it decodes to one: base64 at any alignment, or hex
    (task #1846). Decoding restarts after every '/', so a payload behind a
    host or path prefix of any length (`com/<payload>`, where a compressed
    stream must start at its first byte) keeps `https://evil.io/<payload>`
    flagged."""
    if _hex_carries_payload(blob):
        return False
    starts = [0, *(i + 1 for i, c in enumerate(blob) if c == "/")]
    return not any(_payload_at_any_alignment(blob[start:]) for start in starts)


def _flags_base64(line: str) -> bool:
    """True when some base64-alphabet run on `line` may carry a payload.

    A line can match more than one blob; it only counts as base64 if at least
    one match is not a digest, id list, path, word run (#463, #1372, #1386,
    #1631) or inert part of a URL (#1846)."""
    urls = None
    for m in BASE64_BLOB_RX.finditer(line):
        blob = m.group()
        if _is_benign_base64_shape(blob):
            continue
        if urls is None:
            urls = [u.span() for u in _URL_RX.finditer(line)]
        in_url = any(start <= m.start() and m.end() <= end for start, end in urls)
        if in_url and _is_inert_url_part(blob):
            continue
        return True
    return False


# Credential stores hold opaque tokens: base64/JWT runs are their whole
# content, so the base64 rule can only produce noise there (task #1631:
# ~/.gemini/antigravity-cli/antigravity-oauth-token, jetski-standalone-oauth-
# token). Matched on an extensionless basename, so a `.md` note that merely
# mentions an oauth token stays in scope. Like Excalidraw, this exempts the
# base64 rule ALONE: keyword and hidden-unicode rules still run, so a token
# file carrying an instruction is still reported. A file in the vault inbox is
# an untrusted capture and is never exempt.
_CREDENTIAL_STORE_RX = re.compile(
    r"(^|/)[^/.]*(oauth[-_]?(token|creds|credentials)|(access|refresh|id)[-_]token)[^/.]*$",
    re.IGNORECASE,
)


# A bare token is long and opaque; short words ("hello") are not a token store.
_TOKEN_LINE_RX = re.compile(r"[A-Za-z0-9._~+/=:-]{16,}")
_TOKEN_STORE_MAX_LINES = 4


def _looks_like_token_store(text: str) -> bool:
    """A credential store's content: one JSON document, or a few bare token lines.

    The filename alone is not enough (independent review of PR #928): any
    extensionless file named like `refresh_token`, anywhere in an audited
    tree, would otherwise hide a base64 payload behind prose or markdown.
    """
    body = text.removeprefix("\ufeff").strip()
    if not body:
        return True
    if body[0] in "{[":
        try:
            json.loads(body)
        except ValueError:
            return False
        return True
    lines = body.splitlines()
    return len(lines) <= _TOKEN_STORE_MAX_LINES and all(
        _TOKEN_LINE_RX.fullmatch(line.strip()) for line in lines
    )


# Obsidian Excalidraw stores a drawing's whole scene as a compressed base64
# blob spanning hundreds of lines of a `.excalidraw.md` note. Every one of
# those lines is structurally base64 and can never be anything else, so the
# base64 rule can only ever produce noise there — one live vault returned 499
# INJECTION-* findings, almost all from drawings. Exempt the file type from
# that rule ALONE: the note's text elements are author-controlled prose and
# stay in scope for the keyword and hidden-unicode rules. Note this is a
# structural exemption, not a review: the blob is compressed, so it was never
# human-inspectable in the first place.
EXCALIDRAW_SUFFIXES = (".excalidraw.md", ".excalidraw")


def check_untrusted_injection(relpath: str, text: str):
    """Detect instruction-like patterns, hidden unicode, and base64 blobs in untrusted documents."""
    ext = Path(relpath).suffix.lower()
    in_inbox = "00 - Inbox" in relpath
    is_doc = ext in (".md", ".txt", "") or in_inbox

    if SECRET_SCAN_SKIP.search(relpath) or ext in (".py", ".pyc", ".lock"):
        return

    if not is_doc:
        return

    skip_base64 = relpath.lower().endswith(EXCALIDRAW_SUFFIXES) or (
        not in_inbox
        and bool(_CREDENTIAL_STORE_RX.search(relpath))
        and _looks_like_token_store(text)
    )

    # A base64 blob usually spans many lines; a run of them is one payload and
    # earns one finding, anchored at its first line. Prose between two runs
    # separates them into distinct blobs.
    run_start = 0
    run_len = 0

    def _flush_base64_run():
        nonlocal run_start, run_len
        if not run_len:
            return
        span = f" spanning {run_len} lines" if run_len > 1 else ""
        finding = Finding(
            rule="INJECTION-BASE64",
            severity="medium",
            path=relpath,
            line=run_start,
            message=(
                f"large base64 blob{span} found in document: "
                "potential obfuscated instruction payload"
            ),
            fix="inspect and decode the base64 content to verify it does not contain hidden directives",
        )
        run_start, run_len = 0, 0
        return finding

    body = text.removeprefix("\ufeff")
    for i, line in enumerate(body.splitlines(), 1):
        if SUPPRESS_MARKER in line:
            if run := _flush_base64_run():
                yield run
            continue

        # 1. Hidden unicode
        m = HIDDEN_CHAR_RX.search(line)
        if m:
            if run := _flush_base64_run():
                yield run
            yield Finding(
                rule="INJECTION-HIDDEN-UNICODE",
                severity="medium",
                path=relpath,
                line=i,
                message=f"invisible/bidi Unicode (U+{ord(m.group(0)):04X}) found in document: potential prompt injection",
                fix="inspect the file with a hex viewer and remove the hidden character",
            )
            continue

        # 2. Injection keywords
        m = INJECTION_RX.search(line)
        if m:
            if run := _flush_base64_run():
                yield run
            yield Finding(
                rule="INJECTION-KEYWORD",
                severity="medium",
                path=relpath,
                line=i,
                message=f"possible prompt injection instruction pattern detected ({m.group(0)!r})",
                fix="quarantine the document or remove the instruction-like text",
            )
            continue

        # 3. Base64 blobs — exempt for Excalidraw scenes and credential stores
        # (see EXCALIDRAW_SUFFIXES, _CREDENTIAL_STORE_RX)
        if skip_base64:
            continue
        if _flags_base64(line):
            if not run_len:
                run_start = i
            run_len += 1
        elif run := _flush_base64_run():
            yield run

    if run := _flush_base64_run():
        yield run


CONTENT_RULES.append(check_untrusted_injection)


REPO_RULES = [
    check_agent_settings,
    check_mcp_config,
    check_instructions_exist,
    check_env_exposure,
    check_lockfiles,
    check_gitignore_exists,
]

# The repo rules that judge a file's content (#2225 review r5). The engine
# also runs them on staged blobs at these paths that the working copy did not
# hold; the other repo rules are about the working tree itself (presence,
# .gitignore coverage for future `git add`).
STAGED_REPO_RULES = (check_agent_settings, check_mcp_config)
STAGED_CONFIG_PATHS = frozenset((*AGENT_SETTINGS_PATHS, *MCP_CONFIG_PATHS))
