"""One-shot hub routine scheduler entry point for a systemd/launchd timer."""

import argparse
import json

from . import post_init, storage
from .routines import routine_tick


def main() -> int:
    parser = argparse.ArgumentParser(prog="unified-agent-routines")
    parser.add_argument("command", choices=("tick",))
    parser.add_argument("--force", action="store_true", help="run once despite opt-in switch")
    args = parser.parse_args()
    post_init.register_default_hooks()
    storage.init_db()
    print(json.dumps(routine_tick(force=args.force)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
