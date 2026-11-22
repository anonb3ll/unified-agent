"""Completion reporting for a dispatched agent session (task #601).

The missing link between "a real CLI was spawned" and "the run reached a
terminal state". Before this, ``scheduler.record_run_outcome`` had exactly two
callers: ``worker.py`` (the task #551 smoke worker, whose own docstring says it
"never execs real agent CLIs") and the ``max_attempts_exceeded`` dead-letter
path. A run started by ``CliProcessSpawner`` went ``claimed`` -> ``launched``
and stayed there until its lease expired — while the process was very possibly
still working.

Two ways in, both landing on the same contract:

* **Self-report.** A spawned session runs
  ``unified-agent-dispatch-scheduler report finished``. It needs no arguments:
  ``RoutedLaunchBoundary`` already puts ``UA_DISPATCH_RUN_ID`` and
  ``UA_DISPATCH_AGENT`` into the child's environment, so the session can
  identify its own run. This carries evidence an exit code cannot — a branch,
  a PR URL, a commit.
* **Supervisor.** A parent waiting on the pid maps exit status via
  ``state_for_exit_code`` and reports on the child's behalf (task #602). This
  is the floor: it still fires when the agent crashes, hangs, or simply never
  bothers to report.

Neither is sufficient alone. A self-report is more honest about *what
happened*; only the supervisor is guaranteed to happen at all.

Idempotency is deliberately NOT reimplemented here. ``record_run_outcome``
already refuses an unknown run, a run that is already terminal in a different
state, and a run superseded by a newer live one, and its UPDATE is guarded by
``state NOT IN (...terminal...)`` so a duplicate report cannot write a second
edge. This module resolves identity and attribution, then delegates.
"""

from __future__ import annotations

import os
from typing import Any

from .scheduler import record_run_outcome

#: Env vars ``RoutedLaunchBoundary`` places in a spawned session's environment.
RUN_ID_ENV = "UA_DISPATCH_RUN_ID"
AGENT_ENV = "UA_DISPATCH_AGENT"

#: What a supervised process's exit status means.
EXIT_OK = "finished"
EXIT_FAILED = "failed"


class NotDispatchedError(RuntimeError):
    """Raised when no run_id was given and none is present in the environment.

    Distinct from a failed report: it means this process was not started by
    dispatch at all, so there is nothing to report against. A human running the
    CLI by hand hits this, and should — silently inventing a run_id would be
    worse than refusing.
    """


def resolve_run_id(run_id: str | None = None) -> str:
    """Explicit run_id wins; otherwise read it from the spawn environment."""
    candidate = (run_id or os.environ.get(RUN_ID_ENV) or "").strip()
    if not candidate:
        raise NotDispatchedError(
            f"no run_id given and {RUN_ID_ENV} is unset — this process was not "
            "started by approved dispatch"
        )
    return candidate


def resolve_actor(actor: str | None = None) -> str | None:
    """Attribute the outcome to the dispatched agent when we can.

    Falls back to ``None`` so ``record_run_outcome`` applies its own default
    rather than this module inventing an identity.
    """
    return (actor or os.environ.get(AGENT_ENV) or "").strip() or None


def state_for_exit_code(returncode: int | None) -> str:
    """Map a supervised process's exit status to an outcome state.

    ``None`` (never observed to exit) is a failure, not a success: an
    unobservable process must not be recorded as having completed its work.
    Non-zero, and signal-terminated children (negative returncode from
    ``subprocess``), are failures too.
    """
    return EXIT_OK if returncode == 0 else EXIT_FAILED


def report_outcome(
    state: str,
    *,
    run_id: str | None = None,
    failure_reason: str | None = None,
    evidence_ref: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Record a terminal outcome for a dispatched run.

    Returns ``record_run_outcome``'s result dict unchanged — including its
    ``ok: False`` refusals (``unknown_run``, ``already_terminal``,
    ``superseded_run``, ``invalid_outcome_state``). Callers should treat a
    refusal as information, not an error to retry blindly: "already terminal"
    usually means the supervisor and the agent both reported, which is the
    system working, not failing.
    """
    resolved_id = resolve_run_id(run_id)
    resolved_actor = resolve_actor(actor)
    kwargs: dict[str, Any] = {
        "failure_reason": failure_reason,
        "evidence_ref": evidence_ref,
    }
    if resolved_actor:
        kwargs["actor"] = resolved_actor
    result = record_run_outcome(resolved_id, state, **kwargs)
    try:
        from .. import change_feed

        change_feed.runs_changed([resolved_id])
    except Exception:
        pass
    return result


def report_exit(
    returncode: int | None,
    *,
    run_id: str | None = None,
    evidence_ref: str | None = None,
    actor: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Supervisor entry point: report an outcome derived from exit status."""
    state = state_for_exit_code(returncode)
    failure_reason = None
    if state == EXIT_FAILED:
        shown = "not observed" if returncode is None else str(returncode)
        failure_reason = f"exit_code:{shown}"
        if detail:
            failure_reason = f"{failure_reason} {detail}"
    return report_outcome(
        state,
        run_id=run_id,
        failure_reason=failure_reason,
        evidence_ref=evidence_ref,
        actor=actor,
    )
