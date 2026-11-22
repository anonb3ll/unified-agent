"""Make doc drift impossible (task #373 Tier 3, 4.4).

Measured 2026-08-11: docs/README.md said "99 tools"; the code registered
119 (later 121, by the time this test was written — two more tools landed
in the same session). Nothing enforced the two ever matching, so the
number just went stale and stayed stale. This test is the enforcement:
if `docs/README.md`'s claimed count and `len(server.REGISTERED_TOOLS)`
ever diverge again, CI fails here instead of the number quietly rotting.

Update both together — the doc's number and this file never move alone.
"""

import re
from pathlib import Path

from unified_agent import server

_DOCS_README = Path(__file__).parent.parent / "docs" / "README.md"
_COUNT_RE = re.compile(r"exposes (\d+) tools")


def test_docs_readme_tool_count_matches_registered_tools():
    text = _DOCS_README.read_text()
    match = _COUNT_RE.search(text)
    assert match, (
        f"docs/README.md no longer contains an 'exposes N tools' claim matching "
        f"{_COUNT_RE.pattern!r} — if the sentence was reworded, update this test's "
        f"regex to match the new phrasing rather than deleting the check."
    )
    documented_count = int(match.group(1))
    actual_count = len(server.REGISTERED_TOOLS)
    assert documented_count == actual_count, (
        f"docs/README.md claims {documented_count} tools, but "
        f"server.REGISTERED_TOOLS has {actual_count}. Update the number in "
        f"docs/README.md to match — do not edit this test to make it pass."
    )


def test_tools_reference_covers_every_registered_tool():
    reference = Path(__file__).parent.parent / "docs" / "tools-reference.md"
    registered = set(server.REGISTERED_TOOLS)
    documented: set[str] = set()
    for line in reference.read_text().splitlines():
        if line.startswith("### "):
            documented.update(set(re.findall(r"`?\b([a-z][a-z0-9_]+)\s*\(", line)) & registered)
    missing = sorted(registered - documented)
    assert not missing, "Undocumented registered tools: " + ", ".join(missing)


def test_tool_catalog_lists_every_registered_tool():
    """Every registered tool name appears backticked in docs/tool-catalog.md (task #1645)."""
    catalog = (Path(__file__).parent.parent / "docs" / "tool-catalog.md").read_text()
    missing = sorted(name for name in server.REGISTERED_TOOLS if f"`{name}`" not in catalog)
    assert not missing, "Tools missing from docs/tool-catalog.md: " + ", ".join(missing)


def test_tool_catalog_count_matches_registered_tools():
    catalog = (Path(__file__).parent.parent / "docs" / "tool-catalog.md").read_text()
    match = re.search(r"registers these (\d+) MCP tools", catalog)
    assert match, "docs/tool-catalog.md lost its 'registers these N MCP tools' sentence"
    assert int(match.group(1)) == len(server.REGISTERED_TOOLS)
