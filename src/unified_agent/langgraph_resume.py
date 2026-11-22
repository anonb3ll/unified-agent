"""Resume a paused LangGraph research run safely (task #2522, #2498 item q).

Three pieces the research engine's resume needs and the isolated pilot cannot own:

* ``resume_lease``: a lease on the run's checkpoint thread, stored in the
  checkpoint store next to the checkpoints it guards
  (``langgraph_pilot.RESUME_LEASE_SCHEMA``). One resume holds it at a time, and
  a heartbeat renews it while the holder executes. The runner is fenced on the
  lease token, so every checkpoint write, including the one that records the
  completion, checks the token in its own transaction. A holder whose lease
  expired and was taken over is refused at its next write and records nothing.
  The governed call's effect ledger (``langgraph_dispatch._claim_effect``)
  still refuses a second execution of the same idempotency key underneath it.
* ``check_run_access``: before anything is replayed or run, the run's task must
  be visible in the caller's tenant and the caller must own the run or be the
  verified human operator.
* ``ResearchResumeAdapter``: the governed gateway for every research resume. Its
  one validation, ``revalidate_resume_authority``, applies to the owner and the
  operator alike: the run's saved profile must still be the owner's profile, the
  owner's ceiling must still expose the run's tools, and the task, claim and
  budget preconditions must hold. The operator may only trigger the owner's
  run; its own profile and route grant are never consulted.

``langgraph_dispatch`` is not edited because the progress-note and workflow
runners pin its source in their definition digests.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import config, edition, events, profiles, storage
from .langgraph_dispatch import AuthoritySnapshot, UnifiedAgentDispatchAdapter, _parse_timestamp
from .langgraph_pilot import (
    PilotError,
    PilotRun,
    PilotRunner,
    StaleAuthorityError,
    lease_is_live,
)

#: Lease lifetime without a renewal. The heartbeat renews well inside it.
LEASE_TTL_S = 300.0
#: Interval between renewals while the holder executes.
HEARTBEAT_S = 60.0


class ResumeInFlightError(PilotError):
    """Another resume holds this run's lease."""


@dataclass
class ResumeLease:
    run_id: str
    thread_id: str
    holder: str
    token: str
    lost: threading.Event = field(default_factory=threading.Event)


def _connect(runner: PilotRunner) -> sqlite3.Connection:
    # The runner opened (and owner-checked) this exact file; leases use their own
    # short-lived connections so they never contend with the checkpointer's lock.
    return sqlite3.connect(runner.checkpoint_path.resolve(strict=True), timeout=10.0)


@contextmanager
def resume_lease(runner: PilotRunner, run_id: str, *, holder: str) -> Iterator[ResumeLease]:
    """Hold the run's lease and fence ``runner`` on it, or raise ``ResumeInFlightError``."""
    thread_id = f"ua-lg:{run_id}"
    lease = ResumeLease(run_id, thread_id, holder, events.new_ulid())
    now = datetime.now(UTC)
    conn = _connect(runner)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT expires_at FROM langgraph_resume_leases WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is not None and lease_is_live(row[0], now):
                raise ResumeInFlightError("another resume of this run is in flight")
            conn.execute(
                "INSERT INTO langgraph_resume_leases "
                "(thread_id, org_id, run_id, holder, token, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET org_id=excluded.org_id, "
                "run_id=excluded.run_id, holder=excluded.holder, token=excluded.token, "
                "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                (
                    thread_id,
                    edition.tenant_org_id(),
                    run_id,
                    holder,
                    lease.token,
                    _stamp(now),
                    _stamp(now + timedelta(seconds=LEASE_TTL_S)),
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    runner.fence(thread_id, lease.token)
    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat, args=(runner, lease, stop), name=f"lg-lease-{run_id}", daemon=True
    )
    beat.start()
    try:
        yield lease
    finally:
        stop.set()
        beat.join(timeout=5.0)
        runner.fence(thread_id, None)
        conn = _connect(runner)
        try:
            conn.execute(
                "DELETE FROM langgraph_resume_leases WHERE thread_id=? AND token=?",
                (thread_id, lease.token),
            )
            conn.commit()
        finally:
            conn.close()


def _heartbeat(runner: PilotRunner, lease: ResumeLease, stop: threading.Event) -> None:
    """Renew the lease while it is still ours and live; stop for good once it is not."""
    while not stop.wait(HEARTBEAT_S):
        now = datetime.now(UTC)
        try:
            conn = _connect(runner)
            try:
                renewed = conn.execute(
                    "UPDATE langgraph_resume_leases SET expires_at=? "
                    "WHERE thread_id=? AND token=? AND expires_at > ?",
                    (
                        _stamp(now + timedelta(seconds=LEASE_TTL_S)),
                        lease.thread_id,
                        lease.token,
                        _stamp(now),
                    ),
                ).rowcount
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            continue  # a busy store is retried on the next beat; the fence still holds
        if renewed != 1:
            lease.lost.set()
            return


def human_operator_resume() -> bool:
    """True when the verified human operator calls through a checked operator route."""
    identity = config.verified_agent_identity()
    return (
        bool(identity)
        and identity == config.operator_label()
        and profiles.http_route_human_operator()
    )


def check_run_access(run: PilotRun) -> tuple[str, str] | None:
    """``(code, reason)`` when the caller may not read or resume ``run``, else None.

    Runs before any replay of a stored outcome: the run's task must be visible in
    the caller's tenant, and the caller must own the run or be the verified operator.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE id=? AND org_id IS ?",
            (run.task_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "task_not_accessible", "the run's task is not visible to this caller"
    identity = config.verified_agent_identity()
    if identity != run.claimed_by and not human_operator_resume():
        return "not_run_owner", "only the run's owner or the operator may resume it"
    return None


def revalidate_resume_authority(
    adapter: UnifiedAgentDispatchAdapter, run: PilotRun
) -> AuthoritySnapshot:
    """The one authority check for a research resume, by its owner or by the operator.

    The run executes under its OWNER's authority either way. The owner's profile
    (their live session profile when they call, else their current default,
    which carries any committed downgrade) must equal the run's saved profile and
    stay inside the owner's ceiling, and it alone decides whether the run's tools
    are exposed. The operator's own profile and route grant are never used.
    """
    run.validate()
    identity = config.verified_agent_identity()
    if not (config.enforce_auth() and identity and profiles._current_session_id()):
        raise StaleAuthorityError("governed dispatch requires an authenticated HTTP hub session")
    if identity == run.claimed_by:
        owner_profile = profiles.current()
    elif human_operator_resume():
        owner_profile = profiles.default_for_identity(run.claimed_by)
    else:
        raise StaleAuthorityError("verified identity no longer matches the task owner")
    if owner_profile != run.expected_profile:
        raise StaleAuthorityError("the owner's profile no longer matches the run precondition")
    if not profiles.overlay_ceiling_allows(run.claimed_by, owner_profile):
        raise StaleAuthorityError("the owner's profile is outside their current ceiling")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, status, updated_at, claimed_by, lock_expires_at, project "
            "FROM tasks WHERE id=? AND org_id IS ?",
            (run.task_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise StaleAuthorityError("canonical task no longer exists")
    if row["status"] != "doing":
        raise StaleAuthorityError("canonical task is no longer doing")
    if row["updated_at"] != run.task_updated_at:
        raise StaleAuthorityError("canonical task revision changed")
    if row["claimed_by"] != run.claimed_by:
        raise StaleAuthorityError("canonical task owner changed")
    if row["lock_expires_at"] != run.claim_expires_at:
        raise StaleAuthorityError("canonical claim lease changed")
    claim_expiry = _parse_timestamp(row["lock_expires_at"])
    if claim_expiry is None or datetime.now(UTC) >= claim_expiry:
        raise StaleAuthorityError("canonical claim lease is expired or invalid")
    missing = [
        capability
        for capability in run.required_capabilities
        if capability not in adapter._tool_registry
        or not profiles.tool_is_allowed_for(owner_profile, capability)
    ]
    if missing:
        raise StaleAuthorityError(
            "required capabilities are no longer exposed: " + ", ".join(sorted(missing))
        )
    try:
        current_budget = adapter._budget_revision(run.task_id)
    except Exception as exc:  # noqa: BLE001 - authority providers fail closed
        raise StaleAuthorityError("canonical budget revision is unavailable") from exc
    if not current_budget or current_budget != run.budget_revision:
        raise StaleAuthorityError("canonical budget revision changed")
    return AuthoritySnapshot(
        task_id=row["id"],
        task_updated_at=row["updated_at"],
        claimed_by=row["claimed_by"],
        claim_expires_at=row["lock_expires_at"],
        project=row["project"],
        profile=owner_profile,
        identity=str(identity),
        budget_revision=current_budget,
    )


class ResearchResumeAdapter(UnifiedAgentDispatchAdapter):
    """The research gateway for a resume, by the owner or the verified operator."""

    def revalidate(self, run: PilotRun) -> AuthoritySnapshot:
        return revalidate_resume_authority(self, run)


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC timestamp, so stored values also compare correctly as text."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _now() -> str:
    return _stamp(datetime.now(UTC))
