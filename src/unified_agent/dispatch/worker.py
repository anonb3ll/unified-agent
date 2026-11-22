"""Bounded fake worker for approved-dispatch e2e smoke (task #551).

Starts an isolated subprocess that never execs real agent CLIs (claude/codex/
agy). Persistence, routing, and claim logic stay real; only the process
boundary is faked. Callers finish the run *after* ``claim_and_dispatch`` has
moved the row to ``launched`` (see ``complete_smoke_run``) so the coordinator
does not record a ``launched`` edge after ``finished``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..tools import tasks as tasks_tool
from . import coordinator
from .scheduler import record_run_outcome

ACTOR_SMOKE = "dispatch-smoke"

# Tiny worker script: write started + evidence markers, exit 0. No network,
# no credentials, no agent binaries.
_FAKE_WORKER_SCRIPT = textwrap.dedent(
    """\
    import json, pathlib, sys
    started = pathlib.Path(sys.argv[1])
    evidence = pathlib.Path(sys.argv[2])
    payload = json.loads(sys.argv[3])
    started.write_text("started\\n", encoding="utf-8")
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    """
)


def evidence_path_for(evidence_dir: Path, run_id: str) -> Path:
    return Path(evidence_dir) / f"{run_id}.evidence.json"


def started_path_for(evidence_dir: Path, run_id: str) -> Path:
    return Path(evidence_dir) / f"{run_id}.started"


def complete_smoke_run(
    run_id: str,
    *,
    evidence_ref: str,
    actor: str = ACTOR_SMOKE,
    append_task_note: bool = True,
) -> dict[str, Any]:
    """Finish a launched smoke run: child task done → callback → durable outcome.

    Order matters: ``task_update(done)`` fires the delegation completion
    callback while the row is still ``in_flight``; ``record_run_outcome`` then
    indexes the terminal run (idempotent if the callback already completed the
    delegation).
    """
    run = coordinator.get_run(run_id)
    if run is None:
        return {"ok": False, "reason": "unknown_run", "run_id": run_id}
    if run["state"] not in {"launched", "running", "claimed"}:
        return {
            "ok": False,
            "reason": "not_live",
            "run_id": run_id,
            "state": run["state"],
        }

    task_id = int(run["task_id"])
    token = config.agent_identity_var.set(actor)
    try:
        task_result = tasks_tool.task_update(
            task_id,
            status="done",
            evidence_ref=evidence_ref,
            worktree=False,
            actor=actor,
        )
    finally:
        config.agent_identity_var.reset(token)
    outcome = record_run_outcome(
        run_id,
        "finished",
        actor=actor,
        evidence_ref=evidence_ref,
        append_task_note=append_task_note,
    )
    return {
        "ok": bool(outcome.get("ok")),
        "run_id": run_id,
        "task_id": task_id,
        "task": task_result,
        "outcome": outcome,
        "evidence_ref": evidence_ref,
    }


def complete_smoke_run_from_dir(
    run_id: str,
    evidence_dir: Path,
    *,
    actor: str = ACTOR_SMOKE,
) -> dict[str, Any]:
    """Complete using the evidence file written by ``FakeWorkerProcessSpawner``."""
    path = evidence_path_for(evidence_dir, run_id)
    if not path.is_file():
        return {
            "ok": False,
            "reason": "missing_evidence",
            "run_id": run_id,
            "expected": str(path),
        }
    return complete_smoke_run(run_id, evidence_ref=str(path.resolve()), actor=actor)


@dataclass
class FakeWorkerProcessSpawner:
    """ProcessSpawner that starts a bounded fake worker subprocess.

    Never launches claude/codex/agy. Writes evidence under ``evidence_dir``.
    Does **not** mark the run finished — callers must invoke
    ``complete_smoke_run`` / ``complete_smoke_run_from_dir`` after the
    coordinator has recorded ``launched``.
    """

    evidence_dir: Path
    timeout_s: float = 10.0
    fail_with: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    started_paths: list[Path] = field(default_factory=list)
    evidence_paths: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Relative dirs break subprocess cwd + path args (evidence/evidence/…).
        self.evidence_dir = Path(self.evidence_dir).resolve()

    def spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        if self.fail_with:
            return {"ok": False, "error": self.fail_with, "spawned": False}

        run_id = str(request.get("run_id") or "unknown")
        task_id = request.get("task_id")
        agent_label = request.get("agent_label")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        started = started_path_for(self.evidence_dir, run_id)
        evidence = evidence_path_for(self.evidence_dir, run_id)
        payload = {
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": agent_label,
            "delegation_id": request.get("delegation_id"),
            "cli_backend": request.get("cli_backend"),
            "profile": request.get("profile"),
            "kind": "fake_dispatch_worker",
            "host": config.current_source_host(),
        }
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _FAKE_WORKER_SCRIPT,
                    str(started),
                    str(evidence),
                    json.dumps(payload, separators=(",", ":"), default=str),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=str(self.evidence_dir),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": "fake_worker_timeout",
                "spawned": True,
                "run_id": run_id,
            }

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "fake_worker_failed")[:200]
            return {
                "ok": False,
                "error": f"fake_worker_exit_{proc.returncode}:{err}",
                "spawned": True,
                "run_id": run_id,
                "pid": None,
            }

        self.started_paths.append(started)
        self.evidence_paths.append(evidence)
        evidence_ref = str(evidence.resolve())
        return {
            "ok": True,
            "stub": False,
            "spawned": True,
            "fake_worker": True,
            "run_id": run_id,
            "task_id": task_id,
            "agent_label": agent_label,
            "cli_backend": request.get("cli_backend"),
            "pid": None,
            "started_path": str(started),
            "evidence_ref": evidence_ref,
        }
