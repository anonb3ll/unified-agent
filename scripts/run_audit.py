#!/usr/bin/env python3
"""Run `unified-agent-audit` with the code of the checkout holding this script.

Usage: <venv>/bin/python scripts/run_audit.py . --fail-on high

The shared venv installs `unified_agent` in editable mode from one checkout.
Its `unified-agent-audit` therefore runs that checkout's rules and engine,
even from a linked worktree, so a worktree's own audit changes were never
exercised by the commit and push gates (task #2204). This entry point puts
`<checkout>/src` first on the import path and refuses to run if the package
still resolves anywhere else. The interpreter only has to provide the
dependencies; any checkout's venv will do.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent
SRC = CHECKOUT / "src"


def main() -> int:
    # This checkout's src goes ahead of everything, including the editable
    # install's path entry in site-packages.
    sys.path.insert(0, str(SRC))
    # Child processes started by the audit import the same code.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    )
    import unified_agent

    origin = Path(unified_agent.__file__).resolve()
    if SRC.resolve() not in origin.parents:
        print(
            f"run_audit: unified_agent resolved to {origin}, not this checkout's {SRC}; "
            "refusing to audit with another checkout's code.",
            file=sys.stderr,
        )
        return 2
    from unified_agent.audit.cli import main as audit_main

    return audit_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
