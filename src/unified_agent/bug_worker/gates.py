"""Deterministic verification gates for bug-worker patch handoff."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..reviewer import _is_red_tier


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationResults:
    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def summary(self) -> str:
        parts = [f"{g.name}:{'ok' if g.passed else 'FAIL'}" for g in self.gates]
        return "; ".join(parts)


def _run(cmd: list[str], *, cwd: Path, timeout_s: int = 600) -> tuple[int, str]:
    from .. import execution_locality

    # Task #2109: gates run project code (make, uv); never on a devices-only hub.
    execution_locality.refuse("bug-worker verification gate")
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        # A failed gate, not a crash: the worker must still record the attempt.
        return 124, f"timed out after {timeout_s}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-4000:]


def run_verification_gates(
    cwd: Path,
    *,
    skip_smoke: bool = False,
    skip_audit: bool = False,
) -> VerificationResults:
    """Run ruff, format check, pytest, optional smoke, and security audit."""
    results = VerificationResults()
    cwd = cwd.resolve()

    rc, detail = _run(["uv", "run", "ruff", "check", "."], cwd=cwd)
    results.gates.append(GateResult("ruff_check", rc == 0, detail if rc else ""))

    rc, detail = _run(["uv", "run", "ruff", "format", "--check", "."], cwd=cwd)
    results.gates.append(GateResult("ruff_format", rc == 0, detail if rc else ""))

    rc, detail = _run(["uv", "run", "pytest", "-q"], cwd=cwd, timeout_s=900)
    results.gates.append(GateResult("pytest", rc == 0, detail if rc else ""))

    if not skip_smoke:
        rc, detail = _run(["make", "smoke"], cwd=cwd, timeout_s=300)
        results.gates.append(GateResult("smoke", rc == 0, detail if rc else ""))

    if not skip_audit:
        rc, detail = _run(
            ["uv", "run", "unified-agent-audit", ".", "--fail-on", "high"],
            cwd=cwd,
        )
        results.gates.append(GateResult("audit", rc == 0, detail if rc else ""))

    return results


def red_tier_blocked(changed_files: list[dict]) -> list[str]:
    """Return red-tier paths that block unattended handoff."""
    return _is_red_tier(changed_files)
