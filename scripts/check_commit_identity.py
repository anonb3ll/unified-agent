#!/usr/bin/env python3
"""Fail when a commit in a range carries an unattributed or host-leaking identity.

Task #2079. When ``user.name``/``user.email`` are unset, git falls back to an
auto-detected identity: the OS username at the machine's host name (for
example ``user <user@machine.lan>``). Such a commit is not attributed to a fleet
label, and it leaks a host name into repository history, which must stay
anonymous. Merge commits are the usual carrier, because ``--author`` on a
commit sets the author only and leaves the committer to git's fallback.

For every commit in ``--range`` (default ``origin/main..HEAD``, merges
included) the author and the committer must each be one of:

* an accepted identity pair, matched as an exact name and address: the
  built-in service pairs ``GitHub <noreply@github.com>`` (GitHub's web-flow
  committer) and ``Claude <noreply@anthropic.com>``, plus ``--allow-identity``
  and ``UA_COMMIT_ALLOWED_IDENTITIES`` (``Name <address>`` entries separated by
  ``;``). A legacy identity already on main, such as the Cursor background
  agent's, is allowed this way rather than by widening the name or address
  lists;
* a fleet label at an allowed address. Allowed addresses come only from
  ``--allow-email`` and ``UA_COMMIT_ALLOWED_EMAILS`` (comma-separated); with
  neither the check exits 2. There is no implicit default (``git config
  user.email`` is never consulted) and no suffix allowance: an address passes
  only if it exactly equals an allowed one. An allowed entry that is malformed
  or host-like is a usage error. Labels are the catalog in
  ``src/unified_agent/config.py`` plus ``UA_AGENT_LABELS``, the operator names
  (``UA_OPERATOR_NAME``, ``UA_OPERATOR_LABEL``, ``UA_OPERATOR_ALIASES``),
  ``UA_COMMIT_ALLOWED_NAMES`` and ``--allow-name``. The author of a GitHub web
  merge is named by the merging account's profile, so that name must be
  configured too: a claimed web-flow committer is not proof of provenance.

Matching is exact after one normalization: surrounding whitespace is
stripped from names and configured entries, and everything is casefolded. A
commit address that contains whitespace or angle brackets (possible in a
crafted commit object) is rejected outright. Correctness does not depend on a
host denylist: the host classes (``.lan``, ``.local``, ``.localdomain``,
``.home``, ``.home.arpa``, ``.internal``, a trailing dot, an IP or bracketed
literal, ``(none)``, a domain without a dot, no ``@``) only choose the reason a
rejected address is reported with, and keep host-like entries out of the
allowlist.

Output privacy is structural, for the output of the script itself. Every
line goes through ``emit()``, which formats one of a fixed set of templates
and accepts only allowlisted field values: counts (ints), commit SHAs
(validated hex from git), role and reason enums, a fixed source enum, and
keyed fingerprints. No substring of argv, the
environment, a path, or git's output is ever printed: the range is "the
requested range", argparse's own error text is replaced by a fixed line, and a
git failure reports only its exit code (or the signal that killed it). If
reporting itself fails, ``main()`` prints one constant line and exits 2, and
the installed ``sys.excepthook``/``sys.unraisablehook`` print the same line
instead of a traceback. There is no raw-output mode; to see the raw
identities, run the ``git log`` command the fix text prints, locally.

The guarantee does not cover the Python interpreter's own startup, which runs
before this script and can echo settings such as ``PYTHONWARNINGS`` or
``PYTHONIOENCODING``. Run the check with ``python3 -I -S`` (ignore ``PYTHON*``
variables, user site and ``site``) or a clean environment wherever the output is
published, such as CI and hooks.

Fingerprints are HMAC-SHA256 under a random key drawn for each run, so a list
of candidate names or addresses cannot be hashed and matched against
published output. The trade-off: fingerprints correlate within one run (the
same identity on several commits shares one), never across runs.

Each git call has a 120 s timeout (the ``run_cmd`` limit). A ``--range`` that
starts with ``-`` (including ``--``) is a usage error.

Exit 0 when every commit passes, 1 on a violation, 2 on a usage or git error.

    python3 -I -S scripts/check_commit_identity.py --allow-email ADDRESS
    python3 -I -S scripts/check_commit_identity.py --allow-email ADDRESS \
        --range origin/main --max-count 200
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = REPO_ROOT / "src" / "unified_agent" / "config.py"
CATALOG_NAME = "_FLEET_AGENT_CATALOG"

# Service identities accepted only as exact (name, address) pairs.
SERVICE_IDENTITIES = frozenset(
    {
        ("github", "noreply@github.com"),
        ("claude", "noreply@anthropic.com"),
    }
)
# Diagnostic classes only: an address passes by exact allowlist match, never
# because it is absent from this list.
HOST_SUFFIX_REASONS = (
    (".localdomain", "host_localdomain"),
    (".local", "host_local"),
    (".lan", "host_lan"),
    (".home.arpa", "host_home_arpa"),
    (".home", "host_home"),
    (".internal", "host_internal"),
)
GIT_TIMEOUT_S = 120

IDENTITY_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>\s]+)>\s*$")
FIELD_SEP = "\x1f"
# Per-run key: fingerprints correlate within a run, never across runs.
_FINGERPRINT_KEY = secrets.token_bytes(32)

# --- the only output path ----------------------------------------------------

REASONS = {
    "no_domain": "email has no domain (auto-detected host identity)",
    "placeholder": "email domain is git's placeholder for an unknown host",
    "bare_host": "email domain is a bare host name (no dot)",
    "host_lan": "email domain is a local host name (<host>.lan)",
    "host_local": "email domain is a local host name (<host>.local)",
    "host_localdomain": "email domain is a local host name (<host>.localdomain)",
    "host_home": "email domain is a local host name (<host>.home)",
    "host_home_arpa": "email domain is a local host name (<host>.home.arpa)",
    "host_internal": "email domain is a local host name (<host>.internal)",
    "trailing_dot": "email domain ends in a dot (a resolver FQDN, not an address)",
    "ip_literal": "email domain is an IP address literal",
    "malformed_address": "email contains whitespace or angle brackets (crafted identity)",
    "email_not_allowed": "email is not an allowed fleet address",
    "name_not_label": "name is not a fleet label or configured name, e.g. an OS username",
}
PROBLEMS = {
    "no_email": (
        "no allowed fleet email: pass --allow-email ADDRESS or set "
        "UA_COMMIT_ALLOWED_EMAILS (git config is never used as a default)"
    ),
    "git_timeout": "git did not finish within the time limit (timed out)",
    "catalog_unreadable": "cannot read the fleet label catalog (src/unified_agent/config.py)",
    "catalog_missing": f"{CATALOG_NAME} not found in src/unified_agent/config.py",
    "git_missing": "cannot run git",
    "bad_record": "unexpected git log record (contents not shown)",
    "internal": "internal error (details not shown)",
    "interrupted": "interrupted",
}
SOURCES = frozenset(
    {
        "--allow-identity",
        "UA_COMMIT_ALLOWED_IDENTITIES",
        "--allow-email",
        "UA_COMMIT_ALLOWED_EMAILS",
    }
)

USAGE = (
    "usage: check_commit_identity.py [-h] [--range RANGE] [--repo PATH] "
    "[--allow-email ADDRESS] [--allow-name NAME] [--allow-identity 'NAME <ADDRESS>'] "
    "[--max-count N]"
)
HELP = f"""{USAGE}

Fail when a commit in a range carries an unattributed or host-leaking identity.

options:
  -h, --help            show this help and exit
  --range RANGE         revision range to check (default: origin/main..HEAD)
  --repo PATH           repository to check (default: the current directory)
  --allow-email ADDRESS allowed fleet address (repeatable; or UA_COMMIT_ALLOWED_EMAILS)
  --allow-name NAME     extra allowed name (repeatable; or UA_COMMIT_ALLOWED_NAMES)
  --allow-identity 'NAME <ADDRESS>'
                        accept this exact pair (repeatable; or UA_COMMIT_ALLOWED_IDENTITIES)
  --max-count N         check only the newest N commits of the range

Output never contains a name, address, path, range or git message; see the
module docstring."""

FIX = """\
Fix: commit as your fleet label, with the fleet address, as author AND committer.
  Last commit:   GIT_COMMITTER_NAME=<Label> GIT_COMMITTER_EMAIL=<fleet address> \\
                 git commit --amend --no-edit --author="<Label> <fleet address>"
  Several:       git -c user.name=<Label> -c user.email=<fleet address> \\
                 rebase -r <base> --exec 'git commit --amend --no-edit --reset-author'
  Prevent it:    git config user.name <Label> && git config user.email <fleet address>
                 (or export GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL and
                 GIT_COMMITTER_NAME/GIT_COMMITTER_EMAIL in the agent's environment).
                 Merge commits take the committer identity too.
  See them:      git log --format='%h %an <%ae> | %cn <%ce>' <range>   (locally only)
Only rewrite commits that are not yet on main. Accept a legitimate extra identity
with --allow-identity "Name <address>" (or UA_COMMIT_ALLOWED_IDENTITIES)."""

TEMPLATES = {
    "ok": ("out", "check_commit_identity: {count} {noun} in the requested range: identities OK"),
    "summary": (
        "err",
        "check_commit_identity: {bad} of {count} {noun} in the requested range "
        "carry an unattributed or host-leaking identity:",
    ),
    "violation": ("err", "  {sha} {role:<9} {reason} [{fp_kind} fp {fp}]"),
    "fix": ("err", FIX),
    "problem": ("err", "check_commit_identity: {problem}"),
    "bad_identity": (
        "err",
        "check_commit_identity: {source} entry {position} is not of the form "
        "'Name <address>' (value not shown) [value fp {fp}]",
    ),
    "bad_email": (
        "err",
        "check_commit_identity: {source} entry {position} is not a usable fleet address "
        "(malformed or host-like; value not shown) [value fp {fp}]",
    ),
    "git_signalled": (
        "err",
        "check_commit_identity: git was terminated by signal {signal} while reading "
        "the requested range",
    ),
    "git_failed": (
        "err",
        "check_commit_identity: git log failed (exit {code}) for the requested range; "
        "check that --repo is a git repository and --range names existing commits",
    ),
    "bad_args": (
        "err",
        f"{USAGE}\ncheck_commit_identity: invalid arguments (values not shown); run with --help",
    ),
    "help": ("out", HELP),
}

# Printed by _write_fallback() when reporting itself fails; a constant, never formatted.
FALLBACK_LINE = "check_commit_identity: internal error (details not shown)\n"

SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
FP_RE = re.compile(r"^[0-9a-f]{10}$")


def _int_field(value: object) -> str:
    if type(value) is not int or value < 0 or value > 10**9:
        raise ValueError("emit: count fields must be small non-negative ints")
    return str(value)


def _code_field(value: object) -> str:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("emit: exit codes and signals are 0..255")
    return str(value)


def _enum_field(allowed):
    def check(value: object) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("emit: value outside its enum")
        return allowed[value] if isinstance(allowed, dict) else value

    return check


def _regex_field(pattern: re.Pattern[str], shorten: int | None = None):
    def check(value: object) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("emit: value does not match its pattern")
        return value[:shorten] if shorten else value

    return check


FIELDS = {
    "count": _int_field,
    "bad": _int_field,
    "position": _int_field,
    "code": _code_field,
    "signal": _code_field,
    "noun": _enum_field(frozenset({"commit", "commits"})),
    "sha": _regex_field(SHA_RE, shorten=12),
    "role": _enum_field(frozenset({"author", "committer"})),
    "reason": _enum_field(REASONS),
    "problem": _enum_field(PROBLEMS),
    "source": _enum_field(SOURCES),
    "fp_kind": _enum_field(frozenset({"email", "name"})),
    "fp": _regex_field(FP_RE),
}


def emit(template: str, **fields: object) -> None:
    """Print one fixed template; every field must pass its allowlist check."""
    if template not in TEMPLATES:
        raise ValueError("emit: unknown template")
    stream, text = TEMPLATES[template]
    rendered = {}
    for key, value in fields.items():
        if key not in FIELDS:
            raise ValueError("emit: unknown field")
        rendered[key] = FIELDS[key](value)
    print(text.format(**rendered), file=sys.stdout if stream == "out" else sys.stderr)


# --- checking ------------------------------------------------------------------


class UsageError(Exception):
    """Configuration or git failure; exit 2. Carries a template, never text."""

    def __init__(self, template: str, **fields: object) -> None:
        super().__init__(template)
        self.template = template
        self.fields = fields


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str


@dataclass(frozen=True)
class Policy:
    emails: frozenset[str]
    names: frozenset[str]
    identities: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class Violation:
    sha: str
    role: str
    reason: str  # a REASONS key
    fp_kind: str  # "email" or "name"
    fp: str


class FixedOutputArgumentParser(argparse.ArgumentParser):
    """argparse whose only output is a fixed usage line; values are never shown."""

    def error(self, message: str):  # type: ignore[override]
        raise UsageError("bad_args")

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        raise UsageError("bad_args")


def normalize(value: str) -> str:
    """The single normalization used for every comparison."""
    return value.strip().casefold()


def fingerprint(value: str) -> str:
    digest = hmac.new(_FINGERPRINT_KEY, normalize(value).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:10]


def is_malformed_address(email: str) -> bool:
    """Whitespace or angle brackets anywhere: not an address git would write."""
    return not email or any(ch.isspace() or ch in "<>" for ch in email)


def _is_ip_literal(domain: str) -> bool:
    if domain.startswith("[") or domain.endswith("]"):
        return True
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return False
    return True


def load_fleet_labels(config_py: Path = CONFIG_PY) -> set[str]:
    """Read the fleet label catalog from config.py without importing it."""
    try:
        tree = ast.parse(config_py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        raise UsageError("problem", problem="catalog_unreadable") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == CATALOG_NAME for t in targets):
                labels = {
                    c.value
                    for c in ast.walk(node.value)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                }
                if labels:
                    return labels
    raise UsageError("problem", problem="catalog_missing")


def host_like_reason(email: str) -> str | None:
    """The REASONS key for a host-shaped address, or None. Diagnostic only."""
    email = normalize(email)
    if "@" not in email:
        return "no_domain"
    domain = email.rsplit("@", 1)[1]
    if not domain or "(none)" in domain:
        return "placeholder"
    if _is_ip_literal(domain):
        return "ip_literal"
    if domain.endswith("."):
        return "trailing_dot"
    if "." not in domain:
        return "bare_host"
    for suffix, reason in HOST_SUFFIX_REASONS:
        if domain.endswith(suffix):
            return reason
    return None


def _split_env(name: str, sep: str = ",") -> list[str]:
    return [p.strip() for p in (os.environ.get(name) or "").split(sep) if p.strip()]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise UsageError("problem", problem="git_timeout") from exc
    except (OSError, ValueError) as exc:
        raise UsageError("problem", problem="git_missing") from exc


def parse_identity(raw: str, source: str, position: int) -> tuple[str, str]:
    """Parse ``Name <address>``; an error names the source, never the value."""
    match = IDENTITY_RE.match(raw)
    if not match or not match.group("name") or host_like_reason(match.group("email")) is not None:
        raise UsageError("bad_identity", source=source, position=position, fp=fingerprint(raw))
    return normalize(match.group("name")), normalize(match.group("email"))


def _allowed_email(raw: str, source: str, position: int) -> str:
    email = normalize(raw)
    if is_malformed_address(email) or host_like_reason(email) is not None:
        raise UsageError("bad_email", source=source, position=position, fp=fingerprint(raw))
    return email


def allowed_emails(cli: list[str]) -> frozenset[str]:
    """Only explicitly configured addresses; there is no implicit default."""
    env = _split_env("UA_COMMIT_ALLOWED_EMAILS")
    emails = {_allowed_email(e, "UA_COMMIT_ALLOWED_EMAILS", i) for i, e in enumerate(env, 1)}
    emails |= {_allowed_email(e, "--allow-email", i) for i, e in enumerate(cli, 1)}
    if not emails:
        raise UsageError("problem", problem="no_email")
    return frozenset(emails)


def allowed_names(cli: list[str]) -> frozenset[str]:
    names = set(load_fleet_labels())
    names |= set(_split_env("UA_AGENT_LABELS", ":"))
    names |= set(_split_env("UA_COMMIT_ALLOWED_NAMES"))
    names |= set(_split_env("UA_OPERATOR_ALIASES"))
    for var in ("UA_OPERATOR_NAME", "UA_OPERATOR_LABEL"):
        if (os.environ.get(var) or "").strip():
            names.add(os.environ[var].strip())
    names |= {n for n in cli if n.strip()}
    return frozenset(normalize(n) for n in names)


def allowed_identities(cli: list[str]) -> frozenset[tuple[str, str]]:
    env = _split_env("UA_COMMIT_ALLOWED_IDENTITIES", ";")
    pairs = {parse_identity(raw, "UA_COMMIT_ALLOWED_IDENTITIES", i) for i, raw in enumerate(env, 1)}
    pairs |= {parse_identity(raw, "--allow-identity", i) for i, raw in enumerate(cli, 1)}
    return SERVICE_IDENTITIES | pairs


def list_commits(repo: Path, rev_range: str, max_count: int | None = None) -> list[Commit]:
    fmt = FIELD_SEP.join(["%H", "%an", "%ae", "%cn", "%ce"])
    limit = [f"--max-count={max_count}"] if max_count else []
    # _run() refuses a range starting with "-"; --end-of-options and the
    # trailing "--" keep git from reading the range as an option or a path.
    result = _git(repo, "log", "-z", f"--format={fmt}", *limit, "--end-of-options", rev_range, "--")
    if result.returncode != 0:
        # git's stderr can carry the repo path or the range: never shown.
        if result.returncode < 0:  # killed by a signal: subprocess reports -signum
            raise UsageError("git_signalled", signal=-result.returncode)
        raise UsageError("git_failed", code=result.returncode)
    commits = []
    for record in result.stdout.split("\0"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(FIELD_SEP)
        if len(fields) != 5 or not SHA_RE.fullmatch(fields[0]):
            raise UsageError("problem", problem="bad_record")
        commits.append(Commit(*fields))
    return commits


def check_identity(name: str, email: str, policy: Policy) -> tuple[str, str, str] | None:
    """(reason key, fingerprint kind, fingerprint) for a rejected identity, or None.

    Acceptance is exact membership only; host classes just pick the reason.
    """
    if is_malformed_address(email):
        return "malformed_address", "email", fingerprint(email)
    norm_name, norm_email = normalize(name), normalize(email)
    if (norm_name, norm_email) in policy.identities:
        return None
    if norm_email not in policy.emails:
        return host_like_reason(email) or "email_not_allowed", "email", fingerprint(email)
    if norm_name not in policy.names:
        return "name_not_label", "name", fingerprint(name)
    return None


def check_commits(commits: list[Commit], policy: Policy) -> list[Violation]:
    violations = []
    for c in commits:
        roles = (
            ("author", c.author_name, c.author_email),
            ("committer", c.committer_name, c.committer_email),
        )
        for role, name, email in roles:
            rejected = check_identity(name, email, policy)
            if rejected:
                violations.append(Violation(c.sha, role, *rejected))
    return violations


def _build_parser() -> FixedOutputArgumentParser:
    parser = FixedOutputArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--range", default="origin/main..HEAD", dest="rev_range")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--allow-email", action="append", default=[])
    parser.add_argument("--allow-name", action="append", default=[])
    parser.add_argument("--allow-identity", action="append", default=[])
    parser.add_argument("--max-count", type=int)
    return parser


def _run(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)
    if args.help:
        emit("help")
        return 0
    if args.max_count is not None and args.max_count < 1:
        raise UsageError("bad_args")
    if not args.rev_range or args.rev_range.startswith("-"):
        raise UsageError("bad_args")  # "--", "-n1", "--all": not a range
    policy = Policy(
        emails=allowed_emails(args.allow_email),
        names=allowed_names(args.allow_name),
        identities=allowed_identities(args.allow_identity),
    )
    commits = list_commits(args.repo, args.rev_range, args.max_count)
    violations = check_commits(commits, policy)
    noun = "commit" if len(commits) == 1 else "commits"
    if not violations:
        emit("ok", count=len(commits), noun=noun)
        return 0
    emit("summary", bad=len({v.sha for v in violations}), count=len(commits), noun=noun)
    for v in violations:
        emit("violation", sha=v.sha, role=v.role, reason=v.reason, fp_kind=v.fp_kind, fp=v.fp)
    emit("fix")
    return 1


def _report(argv: list[str] | None) -> int:
    try:
        return _run(argv)
    except UsageError as exc:
        emit(exc.template, **exc.fields)
        return 2
    except KeyboardInterrupt:
        emit("problem", problem="interrupted")
        return 130
    except Exception:  # a traceback or message could carry an identity
        emit("problem", problem="internal")
        return 2


def _write_fallback() -> None:
    """Last resort when reporting itself fails: one constant line, nothing else."""
    try:
        sys.stderr.write(FALLBACK_LINE)
        sys.stderr.flush()
    except BaseException:  # nothing safe is left to report with
        pass


def _fixed_excepthook(*_args: object) -> None:
    _write_fallback()


def install_fail_closed_hooks() -> None:
    """Replace Python's traceback and unraisable reporters with the fixed line."""
    sys.excepthook = _fixed_excepthook
    sys.unraisablehook = _fixed_excepthook


def main(argv: list[str] | None = None) -> int:
    # The outermost guard: if emit() or a handler fails (for example a field
    # rejected by its validator), print one fixed line and exit 2.
    try:
        return _report(argv)
    except BaseException:
        _write_fallback()
        return 2


if __name__ == "__main__":
    install_fail_closed_hooks()
    sys.exit(main())
