#!/usr/bin/env python3
"""Compare the public redactor with a base revision's over every test string literal.

Task #2093. The oct-block rule must leave every string with no ``kty`` exactly
as the base revision redacts it. This script loads the base revision's
redaction modules with ``git show`` into a temporary package, including the
optional oct-block scanner when that revision has it (standard library only), runs
``redact_sensitive_content`` from both on every string constant in ``tests/``,
and reports how many outputs differ, split by whether the string contains
``kty``. Exit 1 when a string without ``kty`` differs.

Nothing it prints holds a fixture value: differing strings are listed by file
and line, with every run of 12 or more letters, digits, ``-`` and ``_`` masked.

Usage:

  uv run python scripts/redaction_corpus_diff.py              # base origin/main
  uv run python scripts/redaction_corpus_diff.py --base c0fcb4e7 --show 20
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = ("jwk", "redaction")
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{12,}")


def load_base(base: str, into: Path):
    """The base revision's redaction module, as package ``ua_base_telemetry``."""
    package = into / "ua_base_telemetry"
    package.mkdir()
    (package / "__init__.py").write_text("")
    # Pin one tree before discovering/copying modules; a concurrent fetch must
    # not mix dependency presence from one base with source from another.
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base}^{{tree}}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    optional = subprocess.run(
        [
            "git",
            "ls-tree",
            "--name-only",
            revision,
            "--",
            "src/unified_agent/telemetry/oct_blocks.py",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    modules = ("oct_blocks", *MODULES) if optional.stdout.strip() else MODULES
    for name in modules:
        source = subprocess.run(
            ["git", "show", f"{revision}:src/unified_agent/telemetry/{name}.py"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (package / f"{name}.py").write_text(source)
    sys.path.insert(0, str(into))
    return importlib.import_module("ua_base_telemetry.redaction")


def literals(root: Path):
    for path in sorted(root.glob("**/*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield path.relative_to(REPO_ROOT), node.lineno, node.value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="base revision (origin/main)")
    parser.add_argument("--show", type=int, default=10, help="differing kty strings to list")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from unified_agent.telemetry import redaction as head

    with tempfile.TemporaryDirectory() as tmp:
        base = load_base(args.base, Path(tmp))
        counts = {"without": 0, "with": 0, "without_diff": 0, "with_diff": 0}
        shown = []
        for path, line, value in literals(REPO_ROOT / "tests"):
            group = "with" if "kty" in value else "without"
            counts[group] += 1
            limit = len(value) + 1000
            before = base.redact_sensitive_content(value, max_len=limit)
            after = head.redact_sensitive_content(value, max_len=limit)
            if before == after:
                continue
            counts[f"{group}_diff"] += 1
            if group == "without" or len(shown) < args.show:
                shown.append(f"  {group} kty: {path}:{line}: {_LONG_RUN.sub('…', value)[:100]!r}")
    print(f"base {args.base}")
    print(f"strings without kty: {counts['without']}, differing: {counts['without_diff']}")
    print(f"strings with kty:    {counts['with']}, differing: {counts['with_diff']}")
    print("\n".join(shown))
    return 1 if counts["without_diff"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
