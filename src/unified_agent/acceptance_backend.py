"""The ua-node ``acceptance`` backend: a harmless job run before real work (task #2112).

A newly enrolled device exercises the node contract before the hub
gives it real work: launch, progress, a receipt with output it wrote, a hub
cancel it honours, and (hub side) refusal of a revoked registration. This is
the device half. It never starts an agent harness, a model or any subprocess:
it parses a small JSON job spec, emits progress events, sleeps briefly, writes
a bounded file under the item's session directory and reads it back.

Job spec (the item prompt), every field bounded:

``{"acceptance": "complete" | "cancel", "run": "acc-<hex>", "steps": 1-10,
"step_s": 0-10, "hold_s": 0-600}``

* ``complete`` emits ``acceptance_launch``, one ``acceptance_progress`` per
  step, writes ``acceptance-output.txt`` and returns success with a digest the
  hub recomputes from the spec.
* ``cancel`` emits the same launch and progress events, then holds until the
  hub's cancel arrives (``kill_check`` turns true), emits ``acceptance_stopped``
  with ``confirmed: true`` and returns ``killed``. A hold that ends without a
  cancel returns an error, so a cancel that never reached the device fails
  acceptance instead of passing it.

Both modes check ``kill_check`` between short sleep slices, so a cancel or a
node shutdown stops the job within one slice. Pure and dependency-free so the
hub can import ``expected_output`` to verify a receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BACKEND = "acceptance"
MODES = ("complete", "cancel")
OUTPUT_FILE = "acceptance-output.txt"
MAX_STEPS = 10
MAX_STEP_S = 10.0
MAX_HOLD_S = 600.0
MAX_OUTPUT_BYTES = 4096
# Sleep granularity: a cancel or shutdown is observed within one slice.
SLICE_S = 0.5
RECEIPT_PREFIX = "acceptance:"
RUN_RE = re.compile(r"^acc-[0-9a-f]{16}$")


class AcceptanceSpecError(ValueError):
    """The prompt is not a valid acceptance job spec."""


def parse_spec(prompt: Any) -> dict[str, Any]:
    """Validate the job spec. Unknown keys and out-of-range values are refused."""
    try:
        spec = json.loads(prompt) if isinstance(prompt, str) else None
    except ValueError as exc:
        raise AcceptanceSpecError("acceptance spec is not JSON") from exc
    if not isinstance(spec, dict):
        raise AcceptanceSpecError("acceptance spec must be a JSON object")
    unknown = set(spec) - {"acceptance", "run", "steps", "step_s", "hold_s"}
    if unknown:
        raise AcceptanceSpecError(f"unknown acceptance spec keys: {sorted(unknown)}")
    mode = spec.get("acceptance")
    if mode not in MODES:
        raise AcceptanceSpecError(f"acceptance must be one of {MODES}")
    run = spec.get("run")
    if not isinstance(run, str) or not RUN_RE.fullmatch(run):
        raise AcceptanceSpecError("run must be acc-<16 hex>")
    steps = spec.get("steps", 3)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS:
        raise AcceptanceSpecError(f"steps must be an integer from 1 to {MAX_STEPS}")

    def seconds(key: str, default: float, cap: float) -> float:
        value = spec.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise AcceptanceSpecError(f"{key} must be a number")
        if not 0 <= value <= cap:
            raise AcceptanceSpecError(f"{key} must be between 0 and {cap}")
        return float(value)

    return {
        "mode": mode,
        "run": run,
        "steps": steps,
        "step_s": seconds("step_s", 1.0, MAX_STEP_S),
        "hold_s": seconds("hold_s", 0.0, MAX_HOLD_S),
    }


def expected_output(run: str, steps: int) -> bytes:
    """The exact bytes a ``complete`` job writes, so the hub can verify the digest."""
    lines = [f"ua-node acceptance {run} step {i}/{steps}\n" for i in range(1, steps + 1)]
    return "".join(lines).encode("utf-8")[:MAX_OUTPUT_BYTES]


def receipt_text(run: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{RECEIPT_PREFIX}complete run={run} bytes={len(data)} sha256={digest}"


class AcceptanceBackend:
    """Built-in, no-model executor backend for the acceptance job."""

    name = BACKEND

    def __init__(self, *, sleep: Callable[[float], Any] | None = None):
        self._sleep = sleep or time.sleep

    def run(
        self,
        *,
        prompt: str,
        session_dir: str,
        kill_check: Callable[[], bool],
        timeout_s: int = 300,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Run one job. Returns an executor-style result; never raises."""
        try:
            spec = parse_spec(prompt)
        except AcceptanceSpecError as exc:
            return {"status": "error", "error": f"acceptance_spec_invalid: {exc}"}
        emit = on_event or (lambda _event: None)
        # Slices, not wall time, bound the job: an injected no-op sleep in tests
        # still terminates, and a real run never exceeds its timeout.
        budget = max(1, int(max(1, int(timeout_s)) / SLICE_S))

        def wait(seconds: float) -> bool:
            """Sleep ``seconds`` in slices; True as soon as a stop is observed."""
            nonlocal budget
            slices = max(1, int(round(seconds / SLICE_S))) if seconds > 0 else 1
            for _ in range(slices):
                if kill_check():
                    return True
                if budget <= 0:
                    return False
                budget -= 1
                if seconds > 0:
                    self._sleep(min(SLICE_S, seconds))
            return kill_check()

        def stopped(step: int) -> dict[str, Any]:
            emit(
                {
                    "kind": "acceptance_stopped",
                    "data": {"run": spec["run"], "confirmed": True, "at_step": step},
                }
            )
            return {"status": "killed", "text": f"{RECEIPT_PREFIX}stopped run={spec['run']}"}

        emit({"kind": "acceptance_launch", "data": {"run": spec["run"], "mode": spec["mode"]}})
        for step in range(1, spec["steps"] + 1):
            if kill_check():
                return stopped(step - 1)
            emit(
                {
                    "kind": "acceptance_progress",
                    "data": {"run": spec["run"], "step": step, "of": spec["steps"]},
                }
            )
            if wait(spec["step_s"]):
                return stopped(step)
        if spec["mode"] == "cancel":
            # Hold until the hub's cancel arrives through the heartbeat.
            hold = max(spec["hold_s"], SLICE_S)
            if wait(hold):
                return stopped(spec["steps"])
            return {"status": "error", "error": "acceptance_cancel_not_received"}
        data = expected_output(spec["run"], spec["steps"])
        path = Path(session_dir) / OUTPUT_FILE
        try:
            # Exclusive creation rejects symlinks and existing hard links.
            # Read the same descriptor, bounded, without reopening a path.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w+b") as output:
                output.write(data)
                output.flush()
                output.seek(0)
                written = output.read(MAX_OUTPUT_BYTES)
        except OSError as exc:
            return {"status": "error", "error": f"acceptance_output_unwritable: {exc}"[:300]}
        if kill_check():
            return stopped(spec["steps"])
        emit(
            {
                "kind": "acceptance_output",
                "data": {"run": spec["run"], "bytes": len(written)},
            }
        )
        return {"status": "success", "exit_code": 0, "text": receipt_text(spec["run"], written)}
