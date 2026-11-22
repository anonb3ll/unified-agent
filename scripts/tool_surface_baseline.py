#!/usr/bin/env python3
"""The reviewed MCP tool surface: every tool and which profiles can see it (task #2416).

A host caches the tool list when it connects, and the default profile sits
near a 100-tool cap, so an added, removed or re-profiled tool should appear as
a reviewed diff. ``docs/tool-surface-baseline.json`` is that record;
``tests/test_tool_surface_baseline.py`` fails when the code and the record differ.

    python scripts/tool_surface_baseline.py --write   # after a deliberate change
    python scripts/tool_surface_baseline.py           # exit 1 and list any drift
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASELINE = Path(__file__).resolve().parent.parent / "docs" / "tool-surface-baseline.json"
UPDATE_COMMAND = "uv run python scripts/tool_surface_baseline.py --write"


def current() -> dict[str, list[str]]:
    """Each registered tool, with the sorted profiles allowed to call it."""
    from unified_agent import profiles, tool_surface

    names = sorted(tool_surface.registry())
    return {
        name: sorted(p for p in profiles.VALID_PROFILES if profiles.tool_is_allowed_for(p, name))
        for name in names
    }


def load(path: Path = BASELINE) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def render(surface: dict[str, list[str]]) -> str:
    """One tool per line, so a change reads as a one-line diff."""
    rows = [f"  {json.dumps(k)}: {json.dumps(surface[k])}" for k in sorted(surface)]
    return "{\n" + ",\n".join(rows) + "\n}\n"


def drift(expected: dict[str, list[str]], actual: dict[str, list[str]]) -> list[str]:
    """Human-readable differences; empty when the surface matches its baseline."""
    lines = [f"added: {n}" for n in sorted(actual.keys() - expected.keys())]
    lines += [f"removed: {n}" for n in sorted(expected.keys() - actual.keys())]
    for name in sorted(actual.keys() & expected.keys()):
        if actual[name] != expected[name]:
            gained = sorted(set(actual[name]) - set(expected[name]))
            lost = sorted(set(expected[name]) - set(actual[name]))
            lines.append(f"re-profiled: {name} gained {gained} lost {lost}")
    return lines


def main(argv: list[str]) -> int:
    surface = current()
    if "--write" in argv:
        BASELINE.write_text(render(surface), encoding="utf-8")
        print(f"wrote {BASELINE.name}: {len(surface)} tools")
        return 0
    differences = drift(load(), surface)
    if differences:
        print("\n".join(differences))
        print(f"If deliberate, run: {UPDATE_COMMAND}")
        return 1
    print(f"tool surface matches its baseline: {len(surface)} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
