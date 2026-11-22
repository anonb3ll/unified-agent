#!/usr/bin/env python3
"""Keep the test suite from growing back (task #2233). Stdlib only: runs with `python3 -I`.

The suite reached about 70k collected cases and an hour of CI, mostly from
one-file-per-issue regression tests and parametrize grids over dimensions that
do not change behaviour. Two guards stop that coming back:

  check   tree state, also asserted by tests/test_test_budget.py in the suite:
          no new test file named after an issue number (`test_*_<digits>.py`),
          and no test file above LINE_CAP lines. Files that broke either rule
          before the guard are grandfathered below; those lists may only shrink.
  count   per-change count between two Git trees: `count --base REV [--head REV]`.
          Every `def`/`async def test*` in every .py file under tests/ counts,
          at any nesting, helpers and conftest.py included, so renames, moves
          and `def test_` text inside strings cannot skew it. Exits 1 when the
          change adds more than --budget test functions or the count is
          unknown, unless one commit message in --messages ends in a trailer
          block with `Test-Budget: <reason>`. Exits 2 when anything cannot be
          read: a missing revision, an unparsable file or unreadable messages.
          The same step holds the redaction grammar pin (task #2441): a change
          that lengthens a module in GRAMMAR_PINS also exits 1 unless a commit
          message ends in a trailer block with `Redaction-Grammar: <evidence>`.
  merge-parent  the base commit a pull-request merge commit was built on:
          `merge-parent [--rev REV] [--pr-head SHA]`. Prints the first parent
          (the base branch tip at merge time), never the pull request's own
          head, so a count can measure the pull request instead of the base
          branch's own work (task #2430). Exits 2 on a revision that is not a
          two-parent merge commit, or when --pr-head is not one of its parents
          (then the checkout is not that pull request's merge).

What it measures: syntactic test definitions, not collected tests. Only
paths under tests/ that differ between base and head are read (added,
modified, deleted, renamed); unchanged paths count the same on both sides.
Net = sum over changed paths of head count minus base count. That does not
bound collected-test growth: deleting uncollected `test*` helpers offsets
added tests, for example.

Threat model: the count catches accidental test growth by honest pull
requests. It does not interpret how pytest collects. A changed path that is a
symlink in either version, or a changed .py file either version of which spells one of
the UNCOUNTABLE names in any identifier or string literal, makes the count
unknown, and an unknown count fails like an over-budget one. Either side of a
changed path counts: deleting only a `__test__ = False` line keeps the
syntactic count while pytest starts collecting, so removing a construct needs
a trailer too. Names built at runtime (`"__" + "test__"`)
and other deliberate evasion are out of scope; review and CI remain that
backstop.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINE_CAP = 1500
CEILING_STEP = 500
DEFAULT_BUDGET = 60

ISSUE_NUMBERED = re.compile(r"^test_.*_\d{3,}\.py$")
TRAILER_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)[ \t]*:[ \t]*(.*)$")
BUDGET_KEY = "test-budget"

# Issue-numbered test files that predate the guard. Fold each into its
# module's test file when it is next touched, then delete the entry here.
GRANDFATHERED_ISSUE_FILES = frozenset(
    {
        "tests/test_audit_jwk_stand_in_2213.py",
        "tests/test_jwk_comment_ownership_2137.py",
        "tests/test_jwk_js_hash_2142.py",
        "tests/test_jwk_js_identifier_2159.py",
        "tests/test_jwk_js_quoted_braced_2159.py",
        "tests/test_jwk_unpack_operand_2135.py",
        "tests/test_policy_authorization_tenant_fence_1798.py",
        "tests/test_redaction_oct_property_runs_2203.py",
        "tests/test_redaction_oct_uri_tag_2139.py",
        "tests/test_redaction_yaml_flow_continuation_2126.py",
        "tests/test_redaction_yaml_rsa_ec_jwk_2200.py",
        "tests/test_reviewer_provenance_2065.py",
    }
)

# Test files above LINE_CAP when the guard landed, each capped at its line
# count then, rounded up to the next multiple of CEILING_STEP. Lower a ceiling
# (or delete the entry) as the file shrinks; never raise one.
GRANDFATHERED_SIZES = {
    "tests/test_acp_followups.py": 2000,
    "tests/test_approval_ledger_orphans.py": 3500,
    "tests/test_audit.py": 2000,
    "tests/test_audit_transcripts.py": 2500,
    "tests/test_doctor.py": 3500,
    "tests/test_evidence_report.py": 2000,
    "tests/test_executor.py": 3000,
    "tests/test_executor_backends.py": 2000,
    "tests/test_harness_sandbox.py": 2000,
    "tests/test_invocation_application_paths.py": 2000,
    "tests/test_jwk_js_hash_2142.py": 2500,
    "tests/test_jwk_js_quoted_braced_2159.py": 2000,
    "tests/test_monitor.py": 3500,
    "tests/test_node.py": 2000,
    "tests/test_operating_profiles.py": 2000,
    "tests/test_operator_cli.py": 2000,
    "tests/test_planning_loop.py": 2000,
    "tests/test_public_relay.py": 2000,
    "tests/test_redaction_oct_blocks.py": 4500,
    "tests/test_redaction_yaml_rsa_ec_jwk_2200.py": 2000,
    "tests/test_reviewer_pipeline.py": 3500,
    "tests/test_session_credentials.py": 2500,
    "tests/test_session_resume.py": 2000,
    "tests/test_settings.py": 3000,
    "tests/test_sleep_pass.py": 2000,
    "tests/test_wake_idle_session.py": 2000,
    "tests/test_webui_routes.py": 3500,
    "tests/test_worktree_policy.py": 2500,
}

SELF = "scripts/test_budget.py"

# The JWK redactor's grammar modules (task #2441). Each fix taught them one more
# syntax a private member can be written in; a newly reported syntax is now
# answered by the coarse fallback (telemetry/jwk_fallback.py, over-redaction),
# not by a new rule. Each module is pinned at its line count: `check` fails when
# one differs from its pin, and `count` fails a change that lengthens one unless
# a commit ends with `Redaction-Grammar: <evidence>`, citing a leak from this
# deployment's own logs. Raise a pin only in such a change; lower it as a module
# shrinks. The rule: docs/security.md, "JWK redaction scope".
GRAMMAR_KEY = "redaction-grammar"
GRAMMAR_PINS = {
    "src/unified_agent/telemetry/jwk.py": 5384,
    "src/unified_agent/telemetry/oct_blocks.py": 1848,
    "src/unified_agent/telemetry/yaml_jwk.py": 1911,
}


# --- tree state ---------------------------------------------------------------


def ceiling_for(lines: int) -> int:
    """The next multiple of CEILING_STEP strictly above ``lines``."""
    return (lines // CEILING_STEP + 1) * CEILING_STEP


def line_count(path: Path) -> int:
    return len(path.read_bytes().splitlines())


def list_test_files(root: Path) -> list[str]:
    """Test files under tests/ by pytest's default names, as ci_changes.candidates finds them."""
    return sorted(
        {
            str(p.relative_to(root))
            for pattern in ("test_*.py", "*_test.py")
            for p in (root / "tests").rglob(pattern)
        }
    )


def check_tree(
    root: Path = REPO_ROOT,
    issue_files: frozenset[str] = GRANDFATHERED_ISSUE_FILES,
    sizes: dict[str, int] | None = None,
    grammar: dict[str, int] | None = None,
) -> list[str]:
    """Every rule the tree breaks, one message each; empty when it is within budget."""
    sizes = GRANDFATHERED_SIZES if sizes is None else sizes
    if grammar is None:
        grammar = GRAMMAR_PINS if root == REPO_ROOT else {}
    problems: list[str] = []
    files = list_test_files(root)
    present = set(files)
    for rel in files:
        if ISSUE_NUMBERED.match(Path(rel).name) and rel not in issue_files:
            problems.append(
                f"{rel}: test file named after an issue number. Put the test in the "
                "module's existing test file and name it after the behaviour it pins."
            )
        lines = line_count(root / rel)
        if rel in sizes:
            if lines > sizes[rel]:
                problems.append(
                    f"{rel}: {lines} lines, above its grandfathered ceiling of {sizes[rel]}. "
                    "Remove redundant cases or move tests to a better-fitting module file; "
                    "do not raise the ceiling."
                )
        elif lines > LINE_CAP:
            problems.append(
                f"{rel}: {lines} lines, above the {LINE_CAP}-line cap for test files. "
                "Remove redundant cases or split it by behaviour; do not allowlist it."
            )
    for rel in sorted(issue_files - present):
        problems.append(
            f"{rel} no longer exists: remove it from GRANDFATHERED_ISSUE_FILES in {SELF} "
            "(the allowlist may only shrink)."
        )
    for rel, ceiling in sorted(sizes.items()):
        if rel not in present:
            problems.append(
                f"{rel} no longer exists: remove it from GRANDFATHERED_SIZES in {SELF}."
            )
            continue
        lines = line_count(root / rel)
        if lines <= LINE_CAP:
            problems.append(
                f"{rel} is {lines} lines, within the {LINE_CAP}-line cap: remove it from "
                f"GRANDFATHERED_SIZES in {SELF}."
            )
        elif ceiling > ceiling_for(lines):
            problems.append(
                f"{rel} shrank to {lines} lines: lower its GRANDFATHERED_SIZES ceiling in "
                f"{SELF} from {ceiling} to {ceiling_for(lines)}."
            )
    for rel, pin in sorted(grammar.items()):
        if not (root / rel).is_file():
            problems.append(f"{rel} no longer exists: remove it from GRAMMAR_PINS in {SELF}.")
            continue
        lines = line_count(root / rel)
        if lines > pin:
            problems.append(
                f"{rel}: {lines} lines, above its redaction grammar pin of {pin}. A new "
                "syntax is answered by the coarse fallback (telemetry/jwk_fallback.py). A new "
                "grammar rule needs a cited leak: raise the pin in the same change and end a "
                "commit message with the trailer `Redaction-Grammar: <evidence>`."
            )
        elif lines < pin:
            problems.append(
                f"{rel} shrank to {lines} lines: lower its GRAMMAR_PINS entry in {SELF} "
                f"from {pin} to {lines}."
            )
    return problems


# --- per-change count ---------------------------------------------------------


class BudgetError(Exception):
    """Input the count cannot trust; the caller exits 2 rather than pass."""


def _is_parametrize(node: ast.expr) -> bool:
    func = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "parametrize"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
    )


# Names that switch pytest collection on or off, or generate items, in ways a
# static count cannot follow. Spelling one anywhere in either version of a
# changed file makes the count unknown.
UNCOUNTABLE = {
    "__test__": "`__test__` switches pytest collection per module or class",
    "pytest_pycollect_makeitem": "a collection hook generates test items",
    "pytest_collect_file": "a collection hook generates test items",
}


def _strings(tree: ast.AST):
    """Every string an AST holds, from any node type and field.

    Generic over ``ast.iter_fields`` rather than a list of node types, so
    identifiers (Name, Attribute, alias, arg, keyword, Match* captures,
    global/nonlocal) and literals (implicit concatenation and escapes are
    already folded by the parser) are all covered, as are future node types.
    """
    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, bytes):
                    yield item.decode("latin-1")


def count_source(source: bytes, name: str = "<test file>") -> tuple[int, int, list[str]]:
    """(test functions, parametrize decorators, uncountable constructs) in one file.

    Every ``def``/``async def test*`` anywhere in the module counts: in any
    class and under any block. Over-counting a nested helper only makes the
    budget stricter. Nothing is excluded; a construct that would make that
    number wrong is reported instead (see UNCOUNTABLE). Text inside strings is
    never code. Raises BudgetError when the file does not parse.
    """
    try:
        tree = ast.parse(source, filename=name)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise BudgetError(f"{name}: cannot parse ({exc})") from exc
    tests = params = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            tests += 1
            params += sum(map(_is_parametrize, node.decorator_list))
        elif isinstance(node, ast.ClassDef):
            params += sum(map(_is_parametrize, node.decorator_list))
    found = {key for text in _strings(tree) for key in UNCOUNTABLE if key in text}
    return tests, params, [f"{name}: `{key}` ({UNCOUNTABLE[key]})" for key in sorted(found)]


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise BudgetError(f"git {' '.join(args[:2])} failed: {detail or 'no output'}")
    return result.stdout


SYMLINK_MODE = b"120000"


def tree_entries(
    repo: Path, rev: str, paths: tuple[str, ...] = ("tests",)
) -> dict[str, tuple[bytes, bytes, str]]:
    """Every entry under ``paths`` (tests/) at ``rev``: path -> (mode, kind, object id).

    ``ls-tree -z`` paths are raw bytes, never quoted, so any file name counts.
    """
    _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}")
    entries: dict[str, tuple[bytes, bytes, str]] = {}
    for record in _git(repo, "ls-tree", "-r", "-z", rev, "--", *paths).split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, oid = meta.split()
        entries[path.decode("utf-8", "surrogateescape")] = (mode, kind, oid.decode())
    return entries


def _counted(entry: tuple[bytes, bytes, str] | None) -> bool:
    """A regular .py blob: the only entries whose definitions are counted."""
    return entry is not None and entry[1] == b"blob" and entry[0] != SYMLINK_MODE


def read_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    if not oids:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{o}\n" for o in oids).encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = out.find(b"\n", pos)
        fields = out[pos:end].split() if end >= 0 else []
        if len(fields) != 3 or fields[1] != b"blob":
            raise BudgetError(f"git cat-file could not read blob {oid}")
        size = int(fields[2])
        blobs[oid] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def count_change(repo: Path, base: str, head: str) -> dict:
    """Test functions and parametrize decorators that ``head`` adds to and removes from ``base``.

    Only paths that differ between the trees are read, both versions of each;
    a rename is a deleted path plus an added one, so an unchanged move nets
    zero. Unchanged paths contribute equally to both sides and are never read,
    so a construct already on the base cannot affect an unrelated change.
    """
    base_tree, head_tree = tree_entries(repo, base), tree_entries(repo, head)
    changed = sorted(
        path
        for path in set(base_tree) | set(head_tree)
        if base_tree.get(path) != head_tree.get(path)
    )
    sides = (("removed", base, base_tree), ("added", head, head_tree))
    wanted = {
        tree[path][2]
        for _, _, tree in sides
        for path in changed
        if path.endswith(".py") and _counted(tree.get(path))
    }
    contents = read_blobs(repo, sorted(wanted))
    counts: dict = dict.fromkeys(("added", "removed", "param_added", "param_removed"), 0)
    uncountable: list[str] = []
    for path in changed:
        for _, rev, tree in sides:
            if (tree.get(path) or (b"",))[0] == SYMLINK_MODE:
                uncountable.append(f"{rev}:{path}: symlink (the count does not follow symlinks)")
        if not path.endswith(".py"):
            continue
        for side, rev, tree in sides:
            if not _counted(tree.get(path)):
                continue
            tests, params, found = count_source(contents[tree[path][2]], f"{rev}:{path}")
            counts[side] += tests
            counts[f"param_{side}"] += params
            uncountable += found
    counts["files"] = len(changed)
    counts["uncountable"] = uncountable
    return counts


def grammar_growth(
    repo: Path, base: str, head: str, pins: dict[str, int] | None = None
) -> list[tuple[str, int, int]]:
    """Pinned grammar modules ``head`` lengthens: (path, base lines, head lines).

    A module absent from ``base`` counts from zero, so adding one is growth.
    """
    paths = tuple(sorted(GRAMMAR_PINS if pins is None else pins))
    if not paths:
        return []
    base_tree, head_tree = tree_entries(repo, base, paths), tree_entries(repo, head, paths)
    oids = sorted({tree[path][2] for tree in (base_tree, head_tree) for path in tree})
    blobs = read_blobs(repo, oids)
    grown = []
    for path in paths:
        before = len(blobs[base_tree[path][2]].splitlines()) if path in base_tree else 0
        after = len(blobs[head_tree[path][2]].splitlines()) if path in head_tree else 0
        if after > before:
            grown.append((path, before, after))
    return grown


def grammar_report(grown: list[tuple[str, int, int]], evidence: str | None) -> tuple[bool, str]:
    if not grown:
        return True, "redaction grammar: no pinned module grew: OK"
    detail = "redaction grammar: " + "; ".join(f"{p} {a} -> {b} lines" for p, a, b in grown)
    if evidence:
        return True, f"{detail}: allowed by Redaction-Grammar: {evidence}"
    return False, (
        f"{detail}: GRAMMAR GROWTH. A newly reported syntax is answered by the coarse "
        "fallback (telemetry/jwk_fallback.py). A new grammar rule needs a cited leak from "
        "this deployment's own logs: end a commit message with the trailer "
        "`Redaction-Grammar: <evidence>` (docs/security.md, JWK redaction scope)."
    )


def merge_parent(repo: Path, rev: str = "HEAD", pr_head: str | None = None) -> str:
    """The base commit a pull-request merge commit was built on (task #2430).

    GitHub's ``refs/pull/<n>/merge`` commit merges the pull request's head into
    the CURRENT base branch tip, so its parents are that base commit and the
    pull-request head. Counting from ``pull_request.base.sha`` instead measures
    the wrong span once the base branch has moved: that SHA is the base tip at
    the pull request's last synchronize, so every commit the base gained since
    then is credited to the pull request. Task #2430: a PR that changed 3 test
    files was reported as 20, +73 test functions instead of +31.

    Returns the parent that is not ``pr_head``, or the first parent when no
    ``pr_head`` is given. A given ``pr_head`` must be one of the two parents:
    otherwise the checkout is not this pull request's merge commit — say it was
    checked out at a branch that merged the base into itself — and counting
    ``parents[0]..HEAD`` would measure that one merge instead of the pull
    request. Refuses anything but a two-parent merge for the same reason: a
    single-parent base would count one commit's tests and pass any change.
    """
    parents = [
        line.split()[1]
        for line in _git(repo, "cat-file", "commit", rev).decode("utf-8", "replace").splitlines()
        if line.startswith("parent ")
    ]
    if len(parents) != 2:
        raise BudgetError(
            f"{rev} is not a pull-request merge commit ({len(parents)} parent(s)); "
            "pass the merge commit, or count with an explicit --base"
        )
    if not pr_head:
        return parents[0]
    if pr_head not in parents:
        raise BudgetError(
            f"{rev} is not the merge of pull request head {pr_head}; its parents are "
            f"{', '.join(parent[:12] for parent in parents)}. Counting it would measure "
            "a different change than the pull request's"
        )
    return parents[1] if parents[0] == pr_head else parents[0]


def _fallback_trailers(message: str) -> list[tuple[str, str]]:
    """The final paragraph's trailers when every line of it is one (git's strict case)."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", message.strip()) if p.strip()]
    if len(paragraphs) < 2:
        return []
    trailers: list[tuple[str, str]] = []
    for line in paragraphs[-1].splitlines():
        if line[:1] in (" ", "\t") and trailers:
            key, value = trailers[-1]
            trailers[-1] = (key, f"{value} {line.strip()}")
            continue
        match = TRAILER_LINE.match(line)
        if not match:
            return []
        trailers.append((match.group(1), match.group(2).strip()))
    return trailers


def message_trailers(message: str) -> list[tuple[str, str]]:
    """A commit message's final trailer block, from `git interpret-trailers` when git exists."""
    if shutil.which("git") is None:
        return _fallback_trailers(message)
    result = subprocess.run(
        ["git", "interpret-trailers", "--parse", "--no-divider"],
        input=message.encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BudgetError("git interpret-trailers failed: " + result.stderr.decode().strip())
    trailers = []
    for line in result.stdout.decode(errors="replace").splitlines():
        key, _, value = line.partition(":")
        trailers.append((key.strip(), value.strip()))
    return trailers


def override_reason(messages: list[str], trailer: str = BUDGET_KEY) -> str | None:
    """The first non-empty `Test-Budget:` (or ``trailer``) reason, one message at a time."""
    for message in messages:
        for key, value in message_trailers(message):
            if key.lower() == trailer and value:
                return value
    return None


def read_messages(path: Path, fmt: str) -> list[str]:
    """Commit messages, one record each: `git log -z --format=%B`, or a JSON string per line."""
    try:
        raw = path.read_bytes().decode("utf-8", "replace")
    except OSError as exc:
        raise BudgetError(f"cannot read commit messages: {exc}") from exc
    if fmt == "nul":
        return [m for m in raw.split("\0") if m.strip()]
    messages = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BudgetError(f"commit messages: not a JSON string per line ({exc})") from exc
        if not isinstance(value, str):
            raise BudgetError("commit messages: not a JSON string per line")
        messages.append(value)
    return messages


def budget_report(counts: dict, budget: int, reason: str | None) -> tuple[bool, str]:
    net = counts["added"] - counts["removed"]
    params = counts["param_added"] - counts["param_removed"]
    detail = (
        f"test budget: net {net:+d} test functions, net {params:+d} parametrize decorators, "
        f"{counts['files']} changed test files; budget {budget}"
    )
    uncountable = counts.get("uncountable") or []
    if uncountable:
        listed = "; ".join(uncountable)
        detail = (
            f"{detail}; COUNT UNKNOWN, changed test files hold constructs it cannot count: {listed}"
        )
        if reason:
            return True, f"{detail}. Allowed by Test-Budget: {reason}"
        return False, (
            f"{detail}. Remove the construct, or end a commit message with the trailer "
            "`Test-Budget: <reason>` after checking the real test count by hand."
        )
    if net <= budget:
        return True, f"{detail}: OK"
    if reason:
        return True, f"{detail}: over budget, allowed by Test-Budget: {reason}"
    return False, (
        f"{detail}: OVER BUDGET. Put regression tests in the module's existing test file, "
        "parametrize only dimensions that change behaviour, or end a commit message "
        "with the trailer `Test-Budget: <reason>`."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="tree-state rules over tests/")
    merge = sub.add_parser(
        "merge-parent", help="the base commit a pull-request merge commit was built on"
    )
    merge.add_argument("--rev", default="HEAD", help="the merge commit (default HEAD)")
    merge.add_argument("--pr-head", help="the pull request's own head, so its parent is skipped")
    merge.add_argument("--repo", type=Path, default=REPO_ROOT, help="Git repository")
    count = sub.add_parser("count", help="net new test functions between two Git trees")
    count.add_argument("--base", required=True, help="base revision")
    count.add_argument("--head", default="HEAD", help="head revision (default HEAD)")
    count.add_argument("--repo", type=Path, default=REPO_ROOT, help="Git repository")
    count.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    count.add_argument("--messages", type=Path, help="commit messages to search for a trailer")
    count.add_argument(
        "--messages-format",
        choices=("nul", "jsonl"),
        default="nul",
        help="nul: `git log -z --format=%%B`; jsonl: one JSON string per line",
    )
    args = parser.parse_args(argv)

    if args.cmd == "check":
        problems = check_tree()
        for problem in problems:
            print(problem)
        print(f"test budget tree check: {len(problems)} problems")
        return 1 if problems else 0
    if args.cmd == "merge-parent":
        try:
            print(merge_parent(args.repo, args.rev, args.pr_head))
        except BudgetError as exc:
            print(f"test budget: ERROR: {exc}", file=sys.stderr)
            return 2
        return 0
    try:
        counts = count_change(args.repo, args.base, args.head)
        grown = grammar_growth(args.repo, args.base, args.head)
        reason = evidence = None
        if args.messages is not None:
            messages = read_messages(args.messages, args.messages_format)
            reason = override_reason(messages)
            evidence = override_reason(messages, GRAMMAR_KEY)
    except BudgetError as exc:
        print(f"test budget: ERROR: {exc}", file=sys.stderr)
        return 2
    ok, report = budget_report(counts, args.budget, reason)
    print(report)
    grammar_ok, grammar = grammar_report(grown, evidence)
    print(grammar)
    return 0 if ok and grammar_ok else 1


if __name__ == "__main__":
    sys.exit(main())
