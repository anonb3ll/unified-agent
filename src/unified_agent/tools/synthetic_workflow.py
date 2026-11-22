"""Opt-in disposable acceptance workflow; never a live provider adapter."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime

from mcp.server.mcpserver.exceptions import ToolError

from .. import budgets, config, edition, events, gateway, profiles, safety, storage, tool_surface
from ..invocation_service import (
    RECEIPT_SCHEMA,
    InvocationService,
    Observation,
    Principal,
    principal_from_request,
)
from . import approvals, tasks

TOOL = "synthetic_workflow"
CHANNEL = "agent.synthetic_workflow"


def _principal():
    if config.workos_request_is_bound():
        principal = principal_from_request()
        if principal.organization != edition.tenant_org_id():
            raise ToolError("request organization does not match storage")
        return principal
    if profiles.current_session_grant() is not None:
        raise ToolError("scoped session credentials are unsupported by synthetic acceptance")
    actor = config.verified_agent_identity()
    if not actor or config.actor_kind_for_identity(actor) != "agent":
        raise ToolError("a verified agent identity is required")
    return Principal(edition.tenant_org_id() or "single-org", "agent:" + actor, actor)


def _disposable_home():
    try:
        return storage.require_disposable_synthetic_home()
    except storage.LiveStoreError as exc:
        raise ToolError(str(exc)) from exc


def _policy_pin():
    path = config.policy_file_path()
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        content = None
    except OSError as exc:
        raise ToolError("current policy is unreadable") from exc
    return (
        str(path.absolute()),
        hashlib.sha256(content).hexdigest() if content is not None else None,
    )


PROVIDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    operation_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    result TEXT NOT NULL,
    org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_effects_org_id ON effects(org_id);
CREATE TABLE IF NOT EXISTS sends (
    operation_id TEXT NOT NULL,
    org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sends_org_id ON sends(org_id);
"""


def _add_missing_org_id(conn):
    """CREATE TABLE IF NOT EXISTS leaves a pre-org_id provider file unchanged."""
    for table in ("effects", "sends"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "org_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN org_id TEXT")


class _Provider:
    """Fixed local SQLite destination, independent durable synthetic commit."""

    is_synthetic = True

    def __init__(self, home):
        _disposable_home()
        self.path = home / "synthetic-provider.sqlite"
        if self.path.is_symlink():
            raise ToolError("synthetic provider symlinks are refused")
        with sqlite3.connect(self.path) as conn:
            _add_missing_org_id(conn)
            conn.executescript(PROVIDER_SCHEMA)

    def send(self, operation_id, digest, arguments):
        _disposable_home()
        result = {"delivery_id": operation_id, "destination": "synthetic://local"}
        org_id = edition.tenant_org_id()
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO sends (operation_id, org_id) VALUES (?, ?)", (operation_id, org_id)
            )
            existing = conn.execute(
                "SELECT digest,result FROM effects WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ToolError("synthetic provider digest conflict")
                result = json.loads(existing[1])
            else:
                conn.execute(
                    "INSERT INTO effects (operation_id, digest, result, org_id) VALUES (?,?,?,?)",
                    (operation_id, digest, json.dumps(result), org_id),
                )
        return result

    def lookup(self, operation_id, digest):
        _disposable_home()
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT digest,result FROM effects WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            return Observation("unknown")
        if row[0] != digest:
            raise ToolError("synthetic provider digest conflict")
        return Observation("known", json.loads(row[1]))


def synthetic_workflow(
    task_id: int,
    run_id: str,
    client_key: str,
    label: str,
    recovery_fence: int | None = None,
) -> dict:
    """Approve a bounded task note and synthetic delivery, recover, or replay.

    Disabled unless the operator explicitly marks UA_HOME disposable and sets
    UA_SYNTHETIC_WORKFLOW=1. No destination, provider or filesystem path is accepted.
    recovery_fence reconciles factual outcomes only; it never resends.
    Cost remains explicitly unknown. This is an acceptance surface, not live delivery.
    """
    home = _disposable_home()
    principal = _principal()
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,64}", label):
        raise ToolError("label must be 1-64 synthetic fixture characters")
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
        raise ToolError("positive task id required")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 200:
        raise ToolError("bounded run identity required")
    if not isinstance(client_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", client_key):
        raise ToolError("client key must be 1-200 synthetic fixture characters")
    if recovery_fence is not None and (
        isinstance(recovery_fence, bool)
        or not isinstance(recovery_fence, int)
        or recovery_fence < 1
    ):
        raise ToolError("positive recovery fence required")
    payload = {"task_id": task_id, "run_id": run_id, "client_key": client_key, "label": label}
    owned = None
    task_result = None
    completed_replay = False
    factual_read = False
    policy_pin = None
    policy_context = None
    gateway_id = None
    gateway_digest = None

    def current_policy_context(conn=None):
        claims = (
            gateway.resolve_claims(principal.actor)
            if conn is None
            else gateway.resolve_claims_on_connection(conn, principal.actor)
        )
        return (
            profiles.current(),
            tuple(sorted((claim.company, claim.project, claim.task) for claim in claims.claims)),
        )

    def authorize(conn, request, executing):
        if not profiles.tool_is_allowed(TOOL):
            raise ToolError("synthetic workflow is no longer visible in the current profile")
        if _principal() != request.principal:
            raise ToolError("principal changed")
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND org_id IS ?", (task_id, edition.tenant_org_id())
        ).fetchone()
        if task is None:
            raise ToolError("task is outside the current tenant")
        if not factual_read:
            if task["current_run_id"] != run_id or task["claimed_by"] != principal.actor:
                raise ToolError("task/run ownership does not match current principal")
            if task["cancel_requested"]:
                raise ToolError("task cancellation requested")
            if task["status"] != "doing":
                raise ToolError("task run is no longer active")
            expiry = datetime.fromisoformat(task["lock_expires_at"] or "2000-01-01T00:00:00+00:00")
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ToolError("task lease expired")
        if owned is not None:
            approvals.validate_receipt(
                conn, (TOOL, owned["payload"]), owned, require_live=not factual_read
            )
        # Policy evaluation can write denial/approval evidence on its own
        # connection. Evaluate outside the owner lock, then fence that exact
        # source under every owner boundary without a second writer/deadlock.
        if policy_pin is not None and _policy_pin() != policy_pin:
            raise ToolError("current policy changed; retry under current policy")
        if policy_context is not None and current_policy_context(conn) != policy_context:
            raise ToolError("current policy context changed; retry under current authority")
        if executing:
            decision = budgets.evaluate_run(conn, principal.actor, task_id=task_id)
            if decision.stop:
                raise ToolError(decision.stop)

    def evidence(conn, stage, request, operation_id, fence):
        nonlocal task_result
        if owned is None:
            raise ToolError("original approval receipt required")
        native_receipt(conn)
        nonlocal gateway_id, gateway_digest
        if stage == "effect_intent":
            call = gateway._CURRENT.get()
            gateway_digest = gateway.arguments_sha256(call.registered_arguments)
            gateway_id = gateway.record_call_result_on_connection(
                conn,
                TOOL,
                None,
                arguments=call.registered_arguments,
                dedupe_key=f"synthetic-gateway:{operation_id}",
            )
        if gateway_id is None:
            raise ToolError("original canonical gateway proof required")
        events.record_event_once_on_connection(
            conn,
            CHANNEL,
            payload={
                "stage": stage,
                "gateway_event_id": gateway_id,
                "gateway_arguments_sha256": gateway_digest,
                "operation_id": operation_id,
                "fence": fence,
                "task_id": task_id,
                "run_id": run_id,
                "principal": asdict(principal),
                "payload_sha256": request.digest,
                "policy_profile": policy_context[0],
                "policy_context_sha256": hashlib.sha256(
                    json.dumps(policy_context).encode()
                ).hexdigest(),
                "approval": owned,
                "approvals": [
                    {"approval_id": owned["approval_id"], "nonce": owned["_consumption_nonce"]}
                ],
                "task_revision": task_result["updated_at"],
                "task_receipt_key": task_key,
                "cost": {"availability": "unknown", "cost_usd": None},
            },
            originator=principal.actor,
            dedupe_key=f"synthetic:{operation_id}:{stage}:{fence}",
        )
        if stage == "effect_completed":
            events.record_event_once_on_connection(
                conn,
                CHANNEL + ".completed",
                payload={
                    "task_id": task_id,
                    "run_id": run_id,
                    "operation_id": operation_id,
                    "approvals": [
                        {"approval_id": owned["approval_id"], "nonce": owned["_consumption_nonce"]}
                    ],
                    "cost": {"availability": "unknown", "cost_usd": None},
                },
                originator=principal.actor,
                dedupe_key=f"synthetic-completed:{operation_id}",
            )
        # Never restore a consumed pass after a durable dispatch intent. Even a
        # crash between the dispatch marker and send must remain indeterminate.
        approvals.mark_governed_execution()

    storage.init_db_once()
    with storage.connect() as conn:
        conn.execute(
            RECEIPT_SCHEMA.replace(
                "CREATE TABLE invocation_receipts",
                "CREATE TABLE IF NOT EXISTS invocation_receipts",
                1,
            )
        )
    service = InvocationService(storage.connect, authorize, evidence, authority=_principal)
    request = service.request(TOOL, 1, client_key, payload)
    task_key = "synthetic-task:" + hashlib.sha256(json.dumps(request.key).encode()).hexdigest()
    native_arguments = {
        "id": task_id,
        "append_note": f"Synthetic workflow {request.digest}",
        "expected_run_id": run_id,
        "idempotency_key": task_key,
        "worktree": False,
    }

    def native_receipt(conn):
        with gateway.invocation("task_update") as call:
            call.registered_arguments = native_arguments
            with tasks.registered_update_binding():
                native = approvals.bound_task_receipt(conn, task_key, task_id, allow_legacy=False)
        marker = native_arguments["append_note"]
        if (
            native is None
            or native.get("id") != task_id
            or native.get("current_run_id") != run_id
            or native.get("updated_at") != task_result["updated_at"]
            or not isinstance(native.get("notes"), str)
            or native["notes"].splitlines().count(marker) != 1
            or native["notes"].splitlines()[-1] != marker
        ):
            raise ToolError("original selected task receipt unavailable or mismatched")
        return native

    def canonical_gateway(conn):
        proof = conn.execute(
            "SELECT payload FROM event_log WHERE ulid=? AND channel=? AND org_id IS ?",
            (gateway_id, gateway.CHANNEL_GATEWAY_DECISION, edition.tenant_org_id()),
        ).fetchone()
        if proof is None:
            raise ToolError("original canonical gateway proof unavailable")
        payload = json.loads(proof[0])
        if (
            payload.get("tool_name") != TOOL
            or payload.get("caller") != principal.actor
            or payload.get("decision") != "allow"
            or payload.get("args_sha256") != gateway_digest
        ):
            raise ToolError("canonical gateway proof does not match original invocation")

    with storage.connect() as conn:
        previous = service.find(conn, request, "effect")
        completed_replay = previous is not None and previous["state"] == "completed"
        factual_read = previous is not None and (completed_replay or recovery_fence is not None)
        authorize(conn, request, False)
        if previous is not None:
            row = conn.execute(
                "SELECT payload FROM event_log WHERE channel=? AND org_id IS ? "
                "AND json_extract(payload,'$.operation_id')=? "
                "AND json_extract(payload,'$.stage')='effect_intent'",
                (CHANNEL, edition.tenant_org_id(), previous["operation_id"]),
            ).fetchone()
            if row is None:
                raise ToolError("original workflow evidence unavailable")
            original = json.loads(row[0])
            owned = original["approval"]
            task_result = {"updated_at": original["task_revision"]}
            gateway_id = original["gateway_event_id"]
            gateway_digest = original["gateway_arguments_sha256"]
            canonical_gateway(conn)
            native_receipt(conn)
            authorize(conn, request, False)
    with approvals.approval_invocation(TOOL, payload) as receipts:
        if owned is not None:
            # Trusted DB evidence, never a caller-supplied grant. The existing
            # validator enforces principal, nonce, grant, expiry and revocation.
            receipts[(owned["approval_id"], owned["generation"])] = owned
            approvals.mark_governed_execution()
            approvals.adopt_invocation_receipt(TOOL, payload, principal.actor, owned)
        policy_pin = _policy_pin()
        policy_context = current_policy_context()
        if factual_read:
            with approvals.completed_consumption_read():
                safety.check_policy_gate(TOOL, payload)
        else:
            safety.check_policy_gate(TOOL, payload)
            approvals.check_approval_gate(TOOL, payload, requested_by=principal.actor, force=True)
        if _policy_pin() != policy_pin or current_policy_context() != policy_context:
            raise ToolError("policy source or context changed during evaluation")
        if owned is None:
            owned = next(
                v
                for v in receipts.values()
                if isinstance(v, dict) and v.get("tool") == TOOL and "_consumption_nonce" in v
            )
            with storage.connect() as conn:
                authorize(conn, request, True)
            if recovery_fence is not None:
                raise ToolError("no original effect exists to reconcile")
            approvals.mark_governed_execution()
            task_result = tool_surface.registry()["task_update"](**native_arguments)
        provider = _Provider(home)
        if recovery_fence is None:
            outcome = service.effect(TOOL, 1, client_key, payload, provider)
        else:
            outcome = service.reconcile(
                TOOL, 1, client_key, payload, provider, expected_fence=recovery_fence
            )
    with storage.connect() as conn:
        canonical_gateway(conn)
        gateway.acknowledge_committed_call(TOOL)
        rows = conn.execute(
            "SELECT ulid,payload FROM event_log WHERE channel=? AND org_id IS ? "
            "AND json_extract(payload,'$.operation_id')=? ORDER BY rowid",
            (CHANNEL, edition.tenant_org_id(), outcome.operation_id),
        ).fetchall()
    try:
        rollup = [
            g
            for g in budgets.cost_rollup("task", agent=principal.actor)["groups"]
            if str(g["task"]) == str(task_id)
        ]
        projection = {"availability": "available", "groups": rollup}
    except Exception:
        # Optional projection cannot invalidate the committed effect or turn
        # missing accounting into zero spend. Mandatory cost above stays unknown.
        projection = {"availability": "unknown", "groups": None}
    return {
        "task_id": task_id,
        "run_id": run_id,
        **asdict(outcome),
        "approval_id": owned["approval_id"],
        "task_receipt_key": task_key,
        "evidence_ids": [r["ulid"] for r in rows],
        "cost": {"availability": "unknown", "cost_usd": None},
        "task_cost_rollup": projection,
    }
