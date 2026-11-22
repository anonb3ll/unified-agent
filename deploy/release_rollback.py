#!/usr/bin/env python3
"""CLI utility to rehearse or perform an authorized rollback of a component release.

Usage:
  python3 deploy/release_rollback.py --component unified-agent [--target-id RELEASE_ID] [--authorize] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

src_dir = Path(__file__).resolve().parents[1] / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from unified_agent.release import list_releases, rollback_release  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rehearse or perform authorized release rollback with checksum verification."
    )
    parser.add_argument(
        "--component",
        default="unified-agent",
        help="Component name (e.g. unified-agent, imessage-bridge)",
    )
    parser.add_argument(
        "--target-id",
        default=None,
        help="Target release ID to roll back to (default: previous valid release)",
    )
    parser.add_argument(
        "--releases-dir",
        type=Path,
        default=None,
        help="Base releases directory (default: ~/.unified_agent/releases)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available releases and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate rollback without modifying active symlinks",
    )
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="Authorize live symlink switch to target release",
    )

    args = parser.parse_args()
    rel_dir = args.releases_dir.resolve() if args.releases_dir else None

    if args.list:
        releases = list_releases(releases_base_dir=rel_dir, component=args.component)
        print(json.dumps(releases, indent=2))
        sys.exit(0)

    res = rollback_release(
        releases_base_dir=rel_dir,
        component=args.component,
        target_release_id=args.target_id,
        authorize=args.authorize,
        dry_run=args.dry_run,
    )

    print(json.dumps(res, indent=2))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
