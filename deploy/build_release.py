#!/usr/bin/env python3
"""CLI utility to create an immutable versioned release snapshot.

Usage:
  python3 deploy/build_release.py --component unified-agent --source ~/dev/unified_agent --version v1.0.0 [--dry-run] [--authorize]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add src to sys.path if needed
src_dir = Path(__file__).resolve().parents[1] / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from unified_agent.release import create_release  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an immutable release package with MANIFEST.json checksums."
    )
    parser.add_argument(
        "--component",
        default="unified-agent",
        help="Component name (e.g. unified-agent, imessage-bridge)",
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Path to component source directory",
    )
    parser.add_argument(
        "--version",
        default="v1.0.0",
        help="Release version string",
    )
    parser.add_argument(
        "--releases-dir",
        type=Path,
        default=None,
        help="Base releases directory (default: ~/.unified_agent/releases)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate release creation without creating files or symlinks",
    )
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="Authorize updating the active 'current' symlink",
    )

    args = parser.parse_args()

    res = create_release(
        source_dir=args.source.resolve(),
        releases_base_dir=args.releases_dir.resolve() if args.releases_dir else None,
        component=args.component,
        version=args.version,
        dry_run=args.dry_run,
        authorize=args.authorize,
    )

    import json

    print(json.dumps(res, indent=2))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
