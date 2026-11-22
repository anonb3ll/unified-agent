#!/usr/bin/env python3
"""Thin wrapper for the real-release wedge demo (task #1140).

Usage:
  uv run python scripts/demo/real_release_wedge.py --decision both --sha a4a9f80 --out /tmp/ua-real-release
  uv run python -m unified_agent.demo.real_release_wedge --decision both
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from unified_agent.demo.real_release_wedge import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
