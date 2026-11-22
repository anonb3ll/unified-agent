"""Package entry: ``python -m unified_agent.demo``.

Defaults to the a11y compliance slice (#1131). Pass ``real-release`` as the
first argv token for the task #1140 real-release wedge.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"real-release", "release", "1140"}:
        from .real_release_wedge import main as release_main

        return release_main(args[1:])
    from .a11y_compliance_remediation import main as a11y_main

    return a11y_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
