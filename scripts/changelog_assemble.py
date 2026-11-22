#!/usr/bin/env python3
"""Assemble ``changelog.d/`` fragments into the ``## [Unreleased]`` section (task #1808).

Every pull request used to add a bullet at the top of ``CHANGELOG.md``, so a batch of
squash-merged PRs conflicted on that one spot. Now each PR adds one fragment file
``changelog.d/<id>.<type>.md`` (a name no other PR uses) and the release step runs
this script to fold the fragments into ``CHANGELOG.md`` and delete them.

    python scripts/changelog_assemble.py --check      # validate fragments, change nothing
    python scripts/changelog_assemble.py --dry-run    # print the resulting Unreleased section
    python scripts/changelog_assemble.py              # write CHANGELOG.md, delete fragments

Standard library only. Released sections (``## [0.3.2]`` and below) are never touched, and
existing Unreleased text is kept byte for byte: only the fragment bullets are inserted.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DEFAULT_FRAGMENTS = REPO_ROOT / "changelog.d"

# Keep-a-Changelog order, as used when more than one type has fragments.
TYPES = ("added", "changed", "deprecated", "removed", "fixed", "security")
RANK = {name: i for i, name in enumerate(TYPES)}

UNRELEASED = re.compile(r"^##\s+\[Unreleased\]\s*$", re.IGNORECASE)
H2 = re.compile(r"^##\s")
RELEASE_HEADING = re.compile(r"^##\s+\[")
H3 = re.compile(r"^###\s+(.*?)\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
BULLET = re.compile(r"^[-*+]\s+\S")
ANY_HEADING = re.compile(r"^#{1,6}\s")
# <id>.<type>.md where <id> is a task or PR number, optionally suffixed ("1808", "1808-2").
FRAGMENT_NAME = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)\.(?P<type>[a-z]+)\.md$")
# Files in changelog.d/ that are not fragments (dotfiles such as .gitkeep or .DS_Store too).
IGNORED = {"README.md"}


@dataclass(frozen=True)
class Fragment:
    path: Path
    ident: str
    type: str
    text: str

    @property
    def sort_key(self) -> tuple[int, int, str]:
        digits = re.match(r"\d+", self.ident)
        # Sorted in reverse for assembly: numeric ids first (newest = highest), then any
        # non-numeric ids by name, so a batch reads newest-first like hand-written entries.
        return (1, int(digits.group()), self.path.name) if digits else (0, 0, self.path.name)


def _fragment_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.name not in IGNORED and not p.name.startswith(".")
    )


def _fence(line: str, open_marker: str | None) -> str | None:
    """Track code fences: return the marker still open after ``line`` (None when closed)."""
    match = FENCE.match(line)
    if not match:
        return open_marker
    marker = match.group(1)
    if open_marker is None:
        return marker
    return None if marker == open_marker else open_marker


def check_fragment_text(text: str) -> str | None:
    """Return a problem description for fragment content, or None when it is valid."""
    if not text.strip():
        return "is empty"
    first = next(line for line in text.splitlines() if line.strip())
    if not BULLET.match(first):
        return "must start with a markdown bullet (`- ...`)"
    open_marker: str | None = None
    for line in text.splitlines():
        if open_marker is None and ANY_HEADING.match(line):
            return f"contains a heading line ({line.strip()[:30]!r}); fragments hold bullets only"
        open_marker = _fence(line, open_marker)
    if open_marker is not None:
        return "has a code fence that is never closed"
    return None


def _normalize(text: str) -> str:
    """Drop surrounding blank lines but keep each line's own trailing spaces (hard breaks)."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def load_fragments(directory: Path) -> tuple[list[Fragment], list[str]]:
    """Read and validate every fragment. Returns (fragments in assembly order, problems)."""
    fragments: list[Fragment] = []
    problems: list[str] = []
    for path in _fragment_files(directory):
        match = FRAGMENT_NAME.match(path.name)
        if not match:
            problems.append(f"{path.name}: name must be <id>.<type>.md")
            continue
        ftype = match.group("type")
        if ftype not in RANK:
            problems.append(f"{path.name}: unknown type {ftype!r} (use one of {', '.join(TYPES)})")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            continue
        issue = check_fragment_text(text)
        if issue:
            problems.append(f"{path.name}: {issue}")
            continue
        fragments.append(Fragment(path, match.group("id"), ftype, _normalize(text)))
    # Newest first within a type (stable), types in Keep a Changelog order.
    fragments.sort(key=lambda f: f.sort_key, reverse=True)
    fragments.sort(key=lambda f: RANK[f.type])
    return fragments, problems


@dataclass
class _Block:
    """A run of Unreleased lines: the preamble (title None) or one ``### Type`` subsection."""

    title: str | None
    heading: str | None
    core: list[str] = field(default_factory=list)
    trail: list[str] = field(default_factory=list)

    @property
    def type(self) -> str | None:
        return self.title.lower() if self.title and self.title.lower() in RANK else None


def _split_trail(lines: list[str]) -> tuple[list[str], list[str]]:
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end], lines[end:]


def _parse_blocks(body: list[str]) -> list[_Block]:
    blocks = [_Block(None, None)]
    raw: list[list[str]] = [[]]
    open_marker: str | None = None
    for line in body:
        heading = None if open_marker else H3.match(line)
        open_marker = _fence(line, open_marker)
        if heading:
            blocks.append(_Block(heading.group(1), line))
            raw.append([])
        else:
            raw[-1].append(line)
    for block, lines in zip(blocks, raw, strict=True):
        block.core, block.trail = _split_trail(lines)
    return blocks


def _unreleased_bounds(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if UNRELEASED.match(line)), None)
    if start is None:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' section")
    open_marker: str | None = None
    for i in range(start + 1, len(lines)):
        if open_marker is None and H2.match(lines[i]):
            return start, i
        if open_marker is not None and RELEASE_HEADING.match(lines[i]):
            # An unclosed fence earlier in Unreleased would swallow the next release heading and
            # make us insert into (or past) a released section. Fail closed instead.
            raise ValueError(
                f"CHANGELOG.md line {i + 1}: a release heading sits inside a code fence; "
                "close the unclosed fence in the Unreleased section first"
            )
        open_marker = _fence(lines[i], open_marker)
    if open_marker is not None:
        raise ValueError("CHANGELOG.md has an unclosed code fence in the Unreleased section")
    return start, len(lines)


def _line_sep(text: str) -> str:
    if "\r\n" in text:
        if re.search(r"(?<!\r)\n", text):
            raise ValueError("mixed line endings in CHANGELOG.md; normalize them first")
        return "\r\n"
    return "\n"


def _already_present(body: list[str], frag: Fragment) -> bool:
    """True when the fragment's exact lines already appear, in order, in the Unreleased body."""
    want = frag.text.split("\n")
    return any(body[i : i + len(want)] == want for i in range(len(body) - len(want) + 1))


def assemble_text(text: str, fragments: list[Fragment]) -> str:
    """Return ``text`` with ``fragments`` inserted under ``## [Unreleased]``."""
    sep = _line_sep(text)
    lines = text.split(sep)  # a trailing newline leaves a final "" element, kept as-is
    start, end = _unreleased_bounds(lines)
    body = lines[start + 1 : end]
    # Skip fragments already folded in by an earlier run that died before deleting them.
    fragments = [f for f in fragments if not _already_present(body, f)]
    tail_is_eof = end == len(lines)
    # The final element of an EOF-terminated file is the empty string after the last newline.
    eof_marker: list[str] = []
    if tail_is_eof and body and body[-1] == "":
        eof_marker = [body.pop()]

    blocks = _parse_blocks(body)
    section_trail = blocks[-1].trail  # blank lines that separate Unreleased from what follows
    by_type: dict[str, list[Fragment]] = {}
    for frag in fragments:
        by_type.setdefault(frag.type, []).append(frag)

    for ftype in TYPES:
        frags = by_type.get(ftype)
        if not frags:
            continue
        added = [part for f in frags for part in (*f.text.split("\n"), "")][:-1]
        existing = next((b for b in blocks if b.type == ftype), None)
        if existing is not None:
            # Directly under the heading (after its blank line, if any), like hand-written entries.
            lead = next((i for i, ln in enumerate(existing.core) if ln.strip()), None)
            if lead is None:
                existing.core = ["", *added]
            else:
                existing.core = [*existing.core[:lead], *added, "", *existing.core[lead:]]
        else:
            new = _Block(ftype.title(), f"### {ftype.title()}", ["", *added])
            later = next(
                (
                    i
                    for i, b in enumerate(blocks)
                    if b.type is not None and RANK[b.type] > RANK[ftype]
                ),
                len(blocks),
            )
            blocks.insert(later, new)

    out: list[str] = []
    for i, block in enumerate(blocks):
        last = i == len(blocks) - 1
        if block.heading is not None:
            out.append(block.heading)
        out.extend(block.core)
        if last:
            out.extend(section_trail or ([] if tail_is_eof else [""]))
        else:
            out.extend(block.trail or [""])
    if not blocks[0].core and blocks[0].title is None and not blocks[0].trail and len(blocks) > 1:
        out.insert(0, "")  # heading directly after "## [Unreleased]" keeps its blank line
    return sep.join([*lines[: start + 1], *out, *eof_marker, *lines[end:]])


def unreleased_section(text: str) -> str:
    sep = _line_sep(text)
    lines = text.split(sep)
    start, end = _unreleased_bounds(lines)
    return sep.join(lines[start:end]).rstrip() + sep


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` via a temp file in the same directory (never half-written)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate fragments; change nothing")
    mode.add_argument(
        "--dry-run", action="store_true", help="print the resulting Unreleased section"
    )
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    parser.add_argument("--fragments-dir", type=Path, default=DEFAULT_FRAGMENTS)
    args = parser.parse_args(argv)

    fragments, problems = load_fragments(args.fragments_dir)
    if problems:
        for problem in problems:
            print(f"changelog.d: {problem}", file=sys.stderr)
        return 1
    if args.check:
        print(f"changelog.d: {len(fragments)} fragment(s) valid")
        return 0
    if not fragments:
        print("changelog.d: no fragments to assemble")
        return 0

    original = args.changelog.read_bytes().decode("utf-8")
    try:
        updated = assemble_text(original, fragments)
    except ValueError as exc:
        print(f"changelog.d: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        sys.stdout.write(unreleased_section(updated))
        return 0
    if updated != original:
        _write_atomic(args.changelog, updated.encode("utf-8"))
    # Delete only after the replace. If a delete fails, the leftover fragment is recognised as
    # already folded in on the next run (see _already_present), so a re-run cannot duplicate it.
    failed = []
    for frag in fragments:
        try:
            frag.path.unlink()
        except OSError as exc:
            failed.append(f"{frag.path.name}: could not delete ({exc})")
    print(f"changelog.d: assembled {len(fragments)} fragment(s) into {args.changelog.name}")
    if failed:
        for problem in failed:
            print(f"changelog.d: {problem}", file=sys.stderr)
        print("changelog.d: delete the leftover fragment(s) by hand or re-run", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
