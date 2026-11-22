#!/usr/bin/env python3
"""Thin wrapper for the compliance a11y remediation demo (task #1131).

Usage:
  uv run python scripts/demo/a11y_compliance_remediation.py --decision both --out /tmp/ua-a11y-demo
  uv run python -m unified_agent.demo.a11y_compliance_remediation --decision both
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from unified_agent.demo.a11y_compliance_remediation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
