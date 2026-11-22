#!/usr/bin/env python3
"""Dedicated smoke runner and evidence recorder for MCP 2.x live verification (UA #864).

Executes the unmocked live progress and resource-subscription verification suite
under an isolated temporary environment and outputs structured smoke evidence.

Usage:
  uv run python scripts/mcp_live_smoke.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup_isolated_env(tmp_dir: Path) -> None:
    os.environ["UA_HOME"] = str(tmp_dir / "ua_home")
    os.environ["UA_WORKSPACE"] = str(tmp_dir / "workspace")
    os.environ["UA_VAULT_PATH"] = str(tmp_dir / "vault")
    Path(os.environ["UA_WORKSPACE"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["UA_VAULT_PATH"]).mkdir(parents=True, exist_ok=True)
    os.environ.pop("UA_BRIDGE_DIR", None)
    os.environ["UA_STATE_SYNC"] = "0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MCP 2.x live verification smoke suite")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ua-mcp-smoke-") as tmp:
        _setup_isolated_env(Path(tmp))

        from unified_agent import post_init, storage
        from unified_agent.mcp_live_verification import run_live_verification_sync
        from unified_agent.server import mcp

        post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
        storage.join_init_background_threads()

        report = run_live_verification_sync(server=mcp)

        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.summary())

        return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
