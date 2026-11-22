"""Isolated transaction proof; repository callbacks are not production bindings."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from unified_agent import config, events, org_partition, storage
from unified_agent.invocation_service import (
    RECEIPT_SCHEMA,
    InvocationConflict,
    InvocationDenied,
    InvocationService,
    Observation,
    Principal,
    StaleInvocation,
    TransactionOwnershipError,
    principal_from_request,
)
from unified_agent.tools.tasks import _next_task_revision

PRINCIPAL = Principal("organization-a", "subject-a", "server_agent2")
REVISION = "2000-01-01T00:00:00+00:00"


def task_organization(transaction, principal):
    # This synthetic repository port uses a verified-looking principal to
    # exercise receipts. On a single-org store its own tasks have NULL scope;
    # other principals must still fail the access check.
    if principal.organization == PRINCIPAL.organization and org_partition.is_partitioned(
        transaction
    ):
        return None
    return principal.organization


def access(transaction, request, executing):
    arguments = request.arguments
    if "id" in arguments:
        row = transaction.execute(
            "SELECT id FROM tasks WHERE id=? AND org_id IS ?",
            (arguments["id"], task_organization(transaction, request.principal)),
        ).fetchone()
        if row is None:
            raise InvocationDenied("task is outside this principal's organization")


def evidence(transaction, stage, request, operation_id, fence):
    events.record_event_once_on_connection(
        transaction,
        "test.invocation",
        payload={
            "stage": stage,
            "operation_id": operation_id,
            "organization": request.principal.organization,
        },
        originator=request.principal.actor,
        dedupe_key=f"{operation_id}:{stage}:{fence}",
    )


def title_mutation(transaction, request):
    # A selected fixture repository port over the actual tasks schema. These
    # statements are not substituted for the production task_update function.
    args = request.arguments
    if set(args) != {"id", "title", "expected_updated_at"}:
        raise ValueError("unsupported title-proof fields")
    if not isinstance(args["title"], str) or not args["title"].strip():
        raise ValueError("title must not be empty")
    row = transaction.execute(
        "SELECT updated_at FROM tasks WHERE id=? AND org_id IS ?",
        (args["id"], task_organization(transaction, request.principal)),
    ).fetchone()
    if row["updated_at"] != args["expected_updated_at"]:
        raise InvocationConflict("authority revision changed")
    revision = _next_task_revision(row["updated_at"])
    transaction.execute(
        "UPDATE tasks SET title=?,updated_at=? WHERE id=? AND org_id IS ?",
        (args["title"], revision, args["id"], task_organization(transaction, request.principal)),
    )
    return {"id": args["id"], "title": args["title"], "updated_at": revision}


class DurableSyntheticProvider:
    is_synthetic = True

    def __init__(self, path, *, crash=False):
        self.path = str(path)
        self.crash = crash
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS effects "
                "(operation_id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS sends (operation_id TEXT NOT NULL)")

    def send(self, operation_id, digest, arguments):
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO sends VALUES (?)", (operation_id,))
            existing = conn.execute(
                "SELECT digest,result FROM effects WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                assert existing[0] == digest
                result = json.loads(existing[1])
            else:
                result = {"delivery_id": operation_id, "destination": arguments["destination"]}
                conn.execute(
                    "INSERT INTO effects VALUES (?,?,?)",
                    (operation_id, digest, json.dumps(result)),
                )
        if self.crash:
            raise SystemExit("synthetic process termination after provider commit")
        return result

    def lookup(self, operation_id, digest):
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT digest,result FROM effects WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            return Observation("unknown")
        assert row[0] == digest
        return Observation("known", json.loads(row[1]))

    def counts(self):
        with sqlite3.connect(self.path) as conn:
            return (
                conn.execute("SELECT count(*) FROM effects").fetchone()[0],
                conn.execute("SELECT count(*) FROM sends").fetchone()[0],
            )


@pytest.fixture
def proof(tmp_path):
    storage.init_db()
    conn = storage.connect()
    try:
        conn.executescript(RECEIPT_SCHEMA)
        partitioned = org_partition.is_partitioned(conn)
        for identifier, organization in [(1, "organization-a"), (2, "organization-b"), (3, None)]:
            conn.execute(
                "INSERT INTO tasks (id,title,created_at,updated_at,org_id) VALUES (?,?,?,?,?)",
                (identifier, "original", REVISION, REVISION, None if partitioned else organization),
            )
        conn.commit()
    finally:
        conn.close()
    service = InvocationService(storage.connect, access, evidence, authority=lambda: PRINCIPAL)
    return service, DurableSyntheticProvider(tmp_path / "provider.sqlite")


def title_args(identifier=1, title="updated", revision=REVISION):
    return {"id": identifier, "title": title, "expected_updated_at": revision}


def task_row(identifier=1):
    with storage.connect() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone())


def row_count(table):
    assert table in {"invocation_receipts", "event_log"}
    with storage.connect() as conn:
        if table == "event_log":
            return conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='test.invocation'"
            ).fetchone()[0]
        return conn.execute("SELECT count(*) FROM invocation_receipts").fetchone()[0]


def test_lost_response_replays_before_revision_after_later_edit(proof):
    service, _ = proof
    args = title_args()
    first = service.mutate("task.title", 1, "client-key", args, title_mutation)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET title='later',updated_at='2030-01-01T00:00:00+00:00' WHERE id=1"
        )
    replay = service.mutate("task.title", 1, "client-key", args, title_mutation)
    assert replay.result == first.result and replay.replayed
    assert task_row()["title"] == "later"
    assert row_count("invocation_receipts") == row_count("event_log") == 1


@pytest.mark.parametrize(
    "field,value", [("id", 2), ("title", "different"), ("expected_updated_at", "later")]
)
def test_changed_payload_conflicts_without_mutation(proof, field, value):
    service, _ = proof
    args = title_args()
    service.mutate("task.title", 1, "key", args, title_mutation)
    changed = {**args, field: value}
    with pytest.raises((InvocationConflict, InvocationDenied)):
        service.mutate("task.title", 1, "key", changed, title_mutation)
    assert task_row()["title"] == "updated"
    assert task_row(2)["title"] == "original"
    assert row_count("event_log") == 1


def test_current_access_blocks_completed_replay(proof):
    service, _ = proof
    service.mutate("task.title", 1, "key", title_args(), title_mutation)

    def denied(*args):
        raise InvocationDenied("access revoked")

    revoked = InvocationService(storage.connect, denied, evidence, authority=lambda: PRINCIPAL)
    with pytest.raises(InvocationDenied):
        revoked.mutate("task.title", 1, "key", title_args(), title_mutation)
    # The fixture's single-org mapping must never grant a different principal
    # access to the local task, including when all stored orgs read as NULL.
    foreign = InvocationService(
        storage.connect,
        access,
        evidence,
        authority=lambda: Principal("organization-b", PRINCIPAL.subject, PRINCIPAL.actor),
    )
    with pytest.raises(InvocationDenied):
        foreign.mutate("task.title", 1, "key", title_args(), title_mutation)
    assert task_row()["title"] == "updated"
    assert row_count("invocation_receipts") == 1


def test_structured_tenant_subject_actor_scope_and_prefix_like_keys(proof):
    service, _ = proof
    service.mutate("task.title", 1, "org:organization-b\x1fkey", title_args(), title_mutation)
    principal_b = Principal("organization-b", "subject-a", "server_agent2")
    other = InvocationService(storage.connect, access, evidence, authority=lambda: principal_b)
    other.mutate("task.title", 1, "org:organization-b\x1fkey", title_args(2), title_mutation)
    for principal in [
        Principal("organization-a", "subject-b", "server_agent2"),
        Principal("organization-a", "subject-a", "Desktop_Agent2"),
    ]:
        scoped = InvocationService(
            storage.connect, access, evidence, authority=lambda principal=principal: principal
        )
        args = title_args(revision=task_row()["updated_at"])
        scoped.mutate("task.title", 1, "org:organization-b\x1fkey", args, title_mutation)
    assert row_count("invocation_receipts") == 4
    with pytest.raises(InvocationDenied):
        service.mutate("task.title", 1, "legacy-null", title_args(3), title_mutation)


def test_key_order_and_snapshot_are_stable(proof):
    service, _ = proof
    args = title_args()
    first = service.mutate("task.title", 1, "key", args, title_mutation)
    replay = service.mutate(
        "task.title", 1, "key", dict(reversed(list(args.items()))), title_mutation
    )
    assert replay.result == first.result
    assert row_count("invocation_receipts") == 1


def test_concurrent_matching_replays_use_one_transaction(proof):
    service, _ = proof
    barrier = threading.Barrier(8)

    def execute(_):
        barrier.wait(timeout=10)
        return service.mutate("task.title", 1, "key", title_args(), title_mutation)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(execute, range(8)))
    assert len({outcome.operation_id for outcome in outcomes}) == 1
    assert sum(not outcome.replayed for outcome in outcomes) == 1
    assert row_count("invocation_receipts") == row_count("event_log") == 1


def test_concurrent_conflicting_requests_have_one_winner(proof):
    service, _ = proof
    barrier = threading.Barrier(2)

    def execute(title):
        barrier.wait(timeout=10)
        try:
            return service.mutate("task.title", 1, "key", title_args(title=title), title_mutation)
        except InvocationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, ["first", "second"]))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert row_count("event_log") == 1


@pytest.mark.parametrize(
    "failure",
    ["evidence", "receipt", "port_commit", "port_rollback", "port_script", "port_sql_commit"],
)
def test_required_write_failure_rolls_back_mutation_and_evidence(proof, failure):
    service, _ = proof
    if failure == "receipt":
        with storage.connect() as conn:
            conn.execute(
                "CREATE TRIGGER refuse_receipt BEFORE UPDATE ON invocation_receipts BEGIN SELECT RAISE(ABORT,'synthetic receipt fault'); END"
            )

    def record(transaction, *args):
        evidence(transaction, *args)
        if failure == "evidence":
            raise RuntimeError("synthetic required evidence fault")

    def mutate(transaction, request):
        result = title_mutation(transaction, request)
        if failure == "port_commit":
            transaction.commit()
        elif failure == "port_rollback":
            transaction.rollback()
        elif failure == "port_script":
            transaction.executescript("SELECT 1")
        elif failure == "port_sql_commit":
            transaction.execute("/* owner test */ -- preface\n COMMIT")
        return result

    service = InvocationService(storage.connect, access, record, authority=lambda: PRINCIPAL)
    with pytest.raises((RuntimeError, sqlite3.Error, TransactionOwnershipError)):
        service.mutate("task.title", 1, "key", title_args(), mutate)
    assert task_row()["title"] == "original"
    assert row_count("invocation_receipts") == row_count("event_log") == 0


def test_unfenced_completed_replay_returns_the_receipt(proof):
    """A completed effect replay carries no fence. reconcile rejects None before
    comparing fences; effect returns the stored receipt."""
    service, provider = proof
    args = {"destination": "synthetic-destination", "body": "fixture"}
    done = service.effect("synthetic.notify", 1, "key", args, provider)
    assert done.status == "completed"
    with pytest.raises(ValueError, match="positive integer"):
        service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=None)
    replay = service.effect("synthetic.notify", 1, "key", args, provider)
    assert replay.status == "completed"
    assert replay.result == done.result
    assert replay.operation_id == done.operation_id
    assert replay.fence == done.fence
    assert replay.replayed
    assert provider.counts() == (1, 1)


def test_completed_recovery_checks_the_fence_before_returning(proof):
    service, provider = proof
    args = {"destination": "synthetic-destination", "body": "fixture"}
    done = service.effect("synthetic.notify", 1, "key", args, provider)
    assert done.status == "completed"
    with pytest.raises(StaleInvocation, match="fence"):
        service.reconcile(
            "synthetic.notify", 1, "key", args, provider, expected_fence=done.fence + 1
        )
    recovered = service.reconcile(
        "synthetic.notify", 1, "key", args, provider, expected_fence=done.fence
    )
    assert recovered.status == "completed" and recovered.result == done.result
    replay = service.effect("synthetic.notify", 1, "key", args, provider)
    assert replay.status == "completed" and replay.replayed
    assert provider.counts() == (1, 1)


def test_effect_completed_replay_does_not_send_twice(proof):
    service, provider = proof
    args = {"destination": "synthetic-destination", "body": "fixture"}
    first = service.effect("synthetic.notify", 1, "key", args, provider)
    replay = service.effect("synthetic.notify", 1, "key", args, provider)
    assert replay.result == first.result and replay.replayed
    assert provider.counts() == (1, 1)
    assert row_count("event_log") == 3
    with pytest.raises(InvocationConflict):
        service.effect("synthetic.notify", 1, "key", {**args, "body": "other"}, provider)


@pytest.mark.parametrize("stage", ["effect_intent", "effect_dispatch", "effect_completed"])
def test_effect_evidence_faults_have_explicit_safe_outcomes(proof, stage):
    _, provider = proof

    def record(transaction, observed, *args):
        if stage == observed:
            raise RuntimeError("synthetic ledger failure")
        evidence(transaction, observed, *args)

    service = InvocationService(storage.connect, access, record, authority=lambda: PRINCIPAL)
    args = {"destination": "synthetic-destination", "body": "fixture"}
    if stage != "effect_completed":
        with pytest.raises(RuntimeError):
            service.effect("synthetic.notify", 1, "key", args, provider)
        assert provider.counts() == (0, 0)
    else:
        outcome = service.effect("synthetic.notify", 1, "key", args, provider)
        assert outcome.status == "indeterminate" and provider.counts() == (1, 1)
        resumed = InvocationService(storage.connect, access, evidence, authority=lambda: PRINCIPAL)
        recovered = resumed.reconcile(
            "synthetic.notify", 1, "key", args, provider, expected_fence=outcome.fence
        )
        assert recovered.status == "completed" and provider.counts() == (1, 1)


def test_fresh_process_reconciles_provider_commit_without_resending(proof):
    service, provider = proof
    provider.crash = True
    args = {"destination": "synthetic-destination", "body": "fixture"}
    with pytest.raises(SystemExit):
        service.effect("synthetic.notify", 1, "key", args, provider)
    script = """
import json, os, sqlite3
from unified_agent.invocation_service import InvocationService
from test_invocation_service import PRINCIPAL, DurableSyntheticProvider, access, evidence
connect=lambda: sqlite3.connect(os.environ['PROOF_DB'], timeout=10)
service=InvocationService(connect, access, evidence, authority=lambda: PRINCIPAL)
provider=DurableSyntheticProvider(os.environ['PROVIDER_DB'])
result=service.reconcile('synthetic.notify',1,'key',{'destination':'synthetic-destination','body':'fixture'},provider,expected_fence=1)
assert result.status=='completed'
assert provider.counts()==(1,1)
print('fresh-process reconciliation passed')
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]
        ),
        "UA_HOME": str(config.data_home()),
        "UA_SETTINGS_FILE": "",
        "UA_STATE_SYNC": "0",
        "HF_HUB_OFFLINE": "1",
        "PROOF_DB": str(config.db_path()),
        "PROVIDER_DB": provider.path,
    }
    child = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=20
    )
    assert child.returncode == 0, "fresh-process recovery failed; output intentionally bounded"
    assert child.stdout.strip() == "fresh-process reconciliation passed"
    assert provider.counts() == (1, 1)


def test_recovery_fence_rejects_stale_completion_and_unknown_does_not_send(proof):
    service, provider = proof

    def uncertain(*args):
        raise RuntimeError("synthetic timeout before provider result")

    provider.send = uncertain
    args = {"destination": "synthetic-destination"}
    outcome = service.effect("synthetic.notify", 1, "key", args, provider)
    recovery = service.reconcile(
        "synthetic.notify", 1, "key", args, provider, expected_fence=outcome.fence
    )
    assert recovery.status == "indeterminate" and recovery.fence == 2
    with pytest.raises(StaleInvocation):
        service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=1)
    request = service._request("synthetic.notify", 1, "key", args)
    with pytest.raises(StaleInvocation):
        service._complete(request, outcome.operation_id, 1, {"delivery_id": "stale"})
    assert provider.counts() == (0, 0)


def test_live_provider_is_refused(proof):
    service, provider = proof
    provider.is_synthetic = False
    with pytest.raises(InvocationDenied):
        service.effect("synthetic.notify", 1, "key", {}, provider)
    assert row_count("invocation_receipts") == 0


def test_principal_requires_real_request_binding_not_env_or_metadata(monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "spoofed-org")
    monkeypatch.setenv("UA_AGENT_LABEL", "server_agent2")
    token = config.auth_metadata_var.set(
        {
            "workos_organization_id": "spoofed-org",
            "workos_user_id": "subject",
            "exp": time.time() + 60,
        }
    )
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(InvocationDenied):
            principal_from_request()
    finally:
        config.agent_identity_var.reset(identity)
        config.auth_metadata_var.reset(token)
    binding = config.bind_workos_request_auth(
        identity="server_agent2",
        subject="subject-a",
        organization_id="organization-a",
        session_id="synthetic-session",
        expires_at=time.time() + 60,
    )
    try:
        assert principal_from_request() == PRINCIPAL
    finally:
        config.reset_workos_request_auth(*binding)


def test_expired_bound_principal_fails_closed():
    binding = config.bind_workos_request_auth(
        identity="server_agent2",
        subject="subject-a",
        organization_id="organization-a",
        session_id="synthetic-session",
        expires_at=time.time() - 1,
    )
    try:
        with pytest.raises(InvocationDenied):
            principal_from_request()
    finally:
        config.reset_workos_request_auth(*binding)


@pytest.mark.parametrize(
    "arguments",
    [
        {"nested": {1: "coerced key"}},
        {"number": float("nan")},
        {"number": float("inf")},
        {"tuple": (1, 2)},
    ],
)
def test_non_json_semantic_payload_is_refused_before_receipt(proof, arguments):
    service, _ = proof
    with pytest.raises(ValueError):
        service.mutate("task.title", 1, "key", arguments, title_mutation)
    assert row_count("invocation_receipts") == 0


def test_marker_before_provider_crash_stays_indeterminate_without_send(proof):
    service, provider = proof

    def crash(*args):
        raise SystemExit("synthetic termination before provider dispatch")

    provider.send = crash
    args = {"destination": "synthetic-destination"}
    with pytest.raises(SystemExit):
        service.effect("synthetic.notify", 1, "key", args, provider)
    provider.lookup = lambda *args: Observation("no_effect")
    recovered = service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=1)
    assert recovered.status == "no_effect"
    assert provider.counts() == (0, 0)
    # The confirmed no-effect answer is recorded: later reads report it and
    # neither replay nor another reconciliation sends or reopens the attempt.
    replay = service.effect("synthetic.notify", 1, "key", args, provider)
    assert replay.status == "no_effect" and provider.counts() == (0, 0)
    # The re-check is fenced (#1986): the pre-recovery fence is stale now.
    with pytest.raises(StaleInvocation):
        service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=1)
    again = service.reconcile(
        "synthetic.notify", 1, "key", args, provider, expected_fence=recovered.fence
    )
    assert again.status == "no_effect" and provider.counts() == (0, 0)


def test_no_effect_recheck_refuses_a_stale_fence(proof):
    """A re-check of a recorded no_effect is fenced like any reconcile (#1986)."""
    service, provider = proof

    def crash(*args):
        raise SystemExit("synthetic termination before provider dispatch")

    provider.send = crash
    args = {"destination": "synthetic-destination"}
    with pytest.raises(SystemExit):
        service.effect("synthetic.notify", 1, "key", args, provider)
    lookups: list[str] = []

    def lookup(operation_id, digest):
        lookups.append(operation_id)
        return Observation("no_effect")

    provider.lookup = lookup
    recorded = service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=1)
    assert recorded.status == "no_effect" and recorded.fence == 2
    assert len(lookups) == 1
    for stale in (1, 99):
        with pytest.raises(StaleInvocation):
            service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=stale)
    assert len(lookups) == 1  # a stale fence never reaches the provider
    current = service.reconcile(
        "synthetic.notify", 1, "key", args, provider, expected_fence=recorded.fence
    )
    assert current.status == "no_effect" and current.fence == recorded.fence
    assert len(lookups) == 2 and provider.counts() == (0, 0)


def test_concurrent_effect_attempts_send_once(proof):
    service, provider = proof
    barrier = threading.Barrier(6)

    def call(_):
        barrier.wait(timeout=10)
        return service.effect(
            "synthetic.notify", 1, "key", {"destination": "synthetic-destination"}, provider
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(call, range(6)))
    assert len({outcome.operation_id for outcome in outcomes}) == 1
    assert any(outcome.status == "completed" for outcome in outcomes)
    assert provider.counts() == (1, 1)


def consumed_gate(payload):
    from contextlib import contextmanager

    from unified_agent.tools import approvals

    @contextmanager
    def scope():
        identity = config.agent_identity_var.set(PRINCIPAL.actor)
        try:
            try:
                approvals.check_approval_gate(
                    "task_update", payload, requested_by=PRINCIPAL.actor, force=True
                )
            except approvals.ApprovalPendingError as pending:
                identifier = pending.approval_id
            else:
                raise AssertionError("isolated forced gate must first park a request")
            operator = config.agent_identity_var.set("Desktop_Person")
            try:
                approvals.approval_resolve(identifier, "approve")
            finally:
                config.agent_identity_var.reset(operator)
            with approvals.approval_invocation(
                "task_update", payload, requested_by=PRINCIPAL.actor
            ):
                approvals.check_approval_gate(
                    "task_update", payload, requested_by=PRINCIPAL.actor, force=True
                )
                yield identifier
        finally:
            config.agent_identity_var.reset(identity)

    return scope()


def live_grant_access(transaction, request, executing):
    from unified_agent.tools import approvals

    access(transaction, request, executing)
    if executing:
        approvals.revalidate_invocation_approvals(transaction)
        approvals.mark_governed_execution()


def test_existing_live_approval_validator_preserves_completed_replay(proof):
    args = title_args()
    service = InvocationService(
        storage.connect, live_grant_access, evidence, authority=lambda: PRINCIPAL
    )
    with consumed_gate(args) as identifier:
        first = service.mutate("task.title", 1, "key", args, title_mutation)
        with storage.connect() as conn:
            conn.execute(
                "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (identifier,),
            )
        replay = service.mutate("task.title", 1, "key", args, title_mutation)
        assert replay.result == first.result and replay.replayed
        with storage.connect() as conn:
            count = conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
            ).fetchone()[0]
        assert count == 1


@pytest.mark.parametrize("fault", ["expired", "revoked", "restore_reconsume"])
def test_existing_live_grant_validator_refuses_changed_consumption(proof, fault):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    args = title_args()
    service = InvocationService(
        storage.connect, live_grant_access, evidence, authority=lambda: PRINCIPAL
    )
    with consumed_gate(args) as identifier:
        if fault == "restore_reconsume":
            approvals.restore_consumed_approval(identifier)
            with approvals.approval_invocation("task_update", args, requested_by=PRINCIPAL.actor):
                approvals.check_approval_gate(
                    "task_update", args, requested_by=PRINCIPAL.actor, force=True
                )
        else:
            with storage.connect() as conn:
                if fault == "expired":
                    conn.execute(
                        "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                        (identifier,),
                    )
                else:
                    conn.execute("UPDATE approvals SET status='revoked' WHERE id=?", (identifier,))
        with pytest.raises(ToolError):
            service.mutate("task.title", 1, "key", args, title_mutation)
    assert task_row()["title"] == "original"
    assert row_count("invocation_receipts") == 0


def test_late_landing_send_upgrades_a_premature_no_effect(proof):
    """A no_effect observed while a send was still in flight is corrected (#1973 review).

    The record is never resent or downgraded, but a later reconcile that finds
    the effect landed upgrades it to completed with the provider's result.
    """
    service, provider = proof
    started, release = threading.Event(), threading.Event()
    real_send = provider.send

    def slow_send(operation_id, digest, arguments):
        started.set()
        assert release.wait(timeout=10)
        return real_send(operation_id, digest, arguments)

    def absent_means_no_effect(operation_id, digest):
        seen = DurableSyntheticProvider.lookup(provider, operation_id, digest)
        return seen if seen.status == "known" else Observation("no_effect")

    provider.send = slow_send
    provider.lookup = absent_means_no_effect
    args = {"destination": "synthetic-destination"}
    outcome: dict = {}
    sender = threading.Thread(
        target=lambda: outcome.setdefault(
            "first", service.effect("synthetic.notify", 1, "key", args, provider)
        )
    )
    sender.start()
    assert started.wait(timeout=10)
    early = service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=1)
    assert early.status == "no_effect"
    release.set()
    sender.join(timeout=10)
    assert not sender.is_alive()
    assert outcome["first"].status == "indeterminate"  # its fence was superseded
    later = service.reconcile("synthetic.notify", 1, "key", args, provider, expected_fence=2)
    assert later.status == "completed"
    assert later.result == {"delivery_id": later.operation_id, "destination": args["destination"]}
    replay = service.effect("synthetic.notify", 1, "key", args, provider)
    assert replay.status == "completed"
    assert provider.counts() == (1, 1)  # one effect, one send: nothing was resent
