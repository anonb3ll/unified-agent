"""Device acceptance: a harmless job a ua-node passes before real work (task #2112).

Enrolling a device ends with an acceptance run (epic #2107, "Enroll a device",
step 4). It exercises the node contract without starting an agent
harness or a model:

* ``launch``           the node claimed the job and started the built-in
                       ``acceptance`` backend (``acceptance_launch`` event);
* ``progress``         it streamed progress events while running;
* ``receipt``          it posted a ``finished`` receipt whose output digest the
                       hub recomputes from the job spec;
* ``cancel_confirmed`` a second job, cancelled by the hub once it was running,
                       stopped mid-run: ``killed`` / ``cancelled_by_hub`` with a
                       confirmed ``acceptance_stopped`` event;
* ``revocation``       a claim for this registration is refused once revoked.

``start`` queues the two items through the ordinary node queue, so the device
reaches them through the same claim, heartbeat, events and receipt routes as
real work. The hub cancels the second item as soon as it reports progress
(``on_node_events``, called from ``node_queue.record_node_event``), and
re-evaluates after every acceptance receipt (``on_receipt``). ``evaluate``
reads the items, their events and receipts, and records the outcome in the
``node_acceptance`` side table: ``pending`` | ``accepted`` | ``failed``, with
per-check results, a timestamp and the node's reported ``code_sha``. A run
still pending after ``DEADLINE_S`` fails as ``timed_out``.

Revocation is checked on the hub, against the registry, not by revoking the
device live. A live revoke signs the device out (``oauth_device_manage``) and
needs a fresh ``unified-agent login`` on the device, which an acceptance run
must not do to a device it is about to trust. The probe runs the exact
ownership gate every claim, heartbeat and receipt passes
(``node_registry._owned_node`` / ``_check_binding``) for this node's stored
row, and requires a 403 for a foreign agent identity, a foreign device (when
the node is device-bound), and the node's own agent once that label is
revoked. Nothing is written by the probe.

With ``UA_NODE_REQUIRE_ACCEPTANCE=1`` (default on when hosted) the queue and scheduler
routing refuse every backend but ``acceptance`` for a node whose latest run is
not ``accepted`` (``require_accepted``, ``filter_accepted``). Diagnostics are
node self-report: all-pass remains pending until an independent lifecycle
operator attests the exact run and evidence. No node-returned digest can prove
filesystem execution against a dishonest node.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import acceptance_backend, config, events, node_queue, node_registry, storage, table_schemas
from .node_registry import NodeError, _load, _row

_log = logging.getLogger(__name__)

BACKEND = acceptance_backend.BACKEND
STATES = ("pending", "accepted", "failed")
CHECKS = ("launch", "progress", "receipt", "cancel_confirmed", "revocation")
# A run not finished this long after it started fails as timed_out.
DEADLINE_S = 900
COMPLETE_SPEC = {"steps": 3, "step_s": 1.0}
# The hold outlasts several heartbeat intervals (30 s default), so the hub's
# cancel reaches the device through the ordinary heartbeat relay.
CANCEL_SPEC = {"steps": 2, "step_s": 1.0, "hold_s": 240.0}
COMPLETE_TIMEOUT_S = 120
CANCEL_TIMEOUT_S = 300
# Hub-internal attribution for the cancel the acceptance run issues itself.
ACTOR = "node-acceptance"
# Identities the revocation probe presents; neither can be a real binding.
PROBE_IDENTITY = "ua-acceptance-probe"
PROBE_DEVICE = "ua-acceptance-probe-device"

PASS, FAIL, PENDING = "pass", "fail", "pending"


def ensure_schema(conn: sqlite3.Connection) -> None:
    table_schemas.node_acceptance(conn)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts: Any) -> datetime | None:
    return node_registry.parse_heartbeat(ts)


def _load_row(conn: sqlite3.Connection, node: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT * FROM node_acceptance WHERE node=?", (node,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    out = _row(row)
    if out is not None:
        out["checks"] = _load(out.pop("checks_json")) or {}
    return out


def _node_row(conn: sqlite3.Connection, node: str) -> dict[str, Any]:
    row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (node,)).fetchone())
    if row is None:
        raise NodeError(f"node {node!r} is not registered", status=404)
    return row


def _spec(mode: str, run: str, extra: dict[str, Any]) -> str:
    return json.dumps({"acceptance": mode, "run": run, **extra}, sort_keys=True)


def _record(kind: str, payload: dict[str, Any], actor: str) -> None:
    try:
        events.record_event(node_queue.CHANNEL_NODE, {"kind": kind, **payload}, originator=actor)
    except Exception:  # noqa: BLE001 - the side table is the source of truth
        _log.debug("acceptance event %s not recorded", kind, exc_info=True)


# --- authority -----------------------------------------------------------------


def authorize(actor: str | None, node: str) -> dict[str, Any]:
    """The node's own agent or a lifecycle operator may start or evaluate a run."""
    from . import agent_lifecycle

    if not actor:
        raise NodeError("an authenticated identity is required for device acceptance", 401)
    name = node_registry.validate_node_name(node)
    conn = storage.connect()
    try:
        node_row = _node_row(conn, name)
    finally:
        conn.close()
    if actor != node_row["agent"] and actor not in agent_lifecycle.operators():
        raise NodeError(
            f"only {node_row['agent']!r} (the node's agent) or an operator "
            "(UA_LIFECYCLE_OPERATORS) may run device acceptance on this node",
            status=403,
        )
    return node_row


# --- start -------------------------------------------------------------------


def start(node: Any, *, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """Publish one run and both diagnostic jobs in a single database transaction.

    BEGIN IMMEDIATE fences starts across workers. No node can claim an item
    before its tracking row exists; any insert failure rolls back all three.
    """
    name = node_registry.validate_node_name(node)
    ref = _now(now)
    # Expire an abandoned run before reserving a new one. The transaction below
    # rechecks the latest state, so another start can never overwrite a winner.
    evaluate(name, now=ref)
    callbacks: list = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_schema(conn)
        node_row = _node_row(conn, name)
        existing = _load_row(conn, name)
        if existing and existing["state"] == "pending":
            raise NodeError(
                f"acceptance run {existing['run_id']} for node {name!r} is still pending; "
                "read diagnostics, attest independently when they pass, or wait for its deadline",
                status=409,
            )
        facts = _load(node_row.get("facts_json")) or {}
        if BACKEND not in (facts.get("backends") or []):
            raise NodeError(
                f"node {name!r} does not advertise the {BACKEND!r} backend; "
                "upgrade ua-node and restart it",
                status=409,
            )
        cwd = node_queue.node_cwd_for_task(facts, None)
        if cwd is None:
            raise NodeError(f"node {name!r} reported no usable worktree root", status=409)
        run_id = f"acc-{secrets.token_hex(8)}"
        items = []
        for mode, spec, timeout in (
            ("complete", COMPLETE_SPEC, COMPLETE_TIMEOUT_S),
            ("cancel", CANCEL_SPEC, CANCEL_TIMEOUT_S),
        ):
            items.append(
                node_queue.enqueue(
                    name,
                    created_by=actor,
                    prompt=_spec(mode, run_id, spec),
                    cwd=cwd,
                    backend=BACKEND,
                    timeout_s=timeout,
                    _transaction=conn,
                    _after_commit=callbacks,
                )
            )
        as_of = _iso(ref)
        checks = {check: _check(PENDING, "not evaluated yet") for check in CHECKS}
        conn.execute(
            "INSERT OR REPLACE INTO node_acceptance (node, run_id, agent, state, "
            "complete_item_id, cancel_item_id, checks_json, reason, code_sha, started_by, "
            "started_at, evaluated_at, accepted_at, updated_at, device_node) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                name,
                run_id,
                node_row["agent"],
                items[0]["item_id"],
                items[1]["item_id"],
                json.dumps(checks),
                node_registry.bounded_code_sha(facts.get("code_sha")),
                actor,
                as_of,
                as_of,
                node_row.get("device_node"),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for callback in callbacks:
        callback()
    _record(
        "acceptance_started",
        {"node": name, "run_id": run_id, "items": [item["item_id"] for item in items]},
        actor,
    )
    return get(name) or {}


# --- evaluate ------------------------------------------------------------------


def _item_events(item_id: str | None) -> list[dict[str, Any]]:
    if not item_id:
        return []
    conn = storage.connect()
    try:
        # Literal channel so the partial index idx_event_log_node_item_turn applies.
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.node' "
            "AND json_extract(payload, '$.item_id')=? "
            "AND json_extract(payload, '$.kind') IN ('acp_event', 'cancel_relayed') ORDER BY ROWID",
            (item_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        payload = _load(row["payload"]) or {}
        data = payload.get("data")
        out.append(
            {
                "kind": payload.get("acp_kind")
                if payload.get("kind") == "acp_event"
                else "hub_cancel_relayed",
                "data": data if isinstance(data, dict) else {},
            }
        )
    return out


def _check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail[:300]}


def _of_run(evts: list[dict[str, Any]], kind: str, run_id: str) -> list[dict[str, Any]]:
    return [e for e in evts if e["kind"] == kind and e["data"].get("run") == run_id]


def _terminal(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("status") in node_queue.TERMINAL_ITEM_STATUSES


def _ordered_progress(evts: list[dict[str, Any]], run_id: str, steps: int) -> bool:
    launched = False
    actual = []
    for event in evts:
        if event["data"].get("run") != run_id:
            continue
        if event["kind"] == "acceptance_launch":
            if launched:
                return False
            launched = True
        elif event["kind"] == "acceptance_progress":
            if not launched or event["data"].get("of") != steps:
                return False
            step = event["data"].get("step")
            if type(step) is not int:
                return False
            actual.append(step)
    return actual == list(range(1, steps + 1))


def _launch_and_progress(
    item: dict[str, Any] | None, evts: list[dict[str, Any]], run_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    launched = _of_run(evts, "acceptance_launch", run_id)
    progress = _of_run(evts, "acceptance_progress", run_id)
    if item is None:
        missing = _check(FAIL, "the acceptance item is gone from the queue")
        return missing, missing
    if launched:
        launch = _check(PASS, f"launched on claim attempt {item.get('attempts')}")
    elif _terminal(item):
        launch = _check(FAIL, f"item ended {item['status']} without a launch event")
    else:
        launch = _check(PENDING, f"item is {item['status']}")
    steps = COMPLETE_SPEC["steps"]
    ordered = _ordered_progress(evts, run_id, steps)
    if ordered:
        prog = _check(PASS, f"{steps} unique ordered progress steps after launch")
    elif _terminal(item):
        prog = _check(FAIL, f"{len(progress)} of {steps} progress event(s) before it ended")
    else:
        prog = _check(PENDING, f"{len(progress)} of {steps} progress event(s) so far")
    return launch, prog


def _receipt_check(item: dict[str, Any] | None, run_id: str) -> dict[str, str]:
    if item is None:
        return _check(FAIL, "the acceptance item is gone from the queue")
    if not _terminal(item):
        return _check(PENDING, f"item is {item['status']}")
    receipt = item.get("receipt") or {}
    expected = acceptance_backend.receipt_text(
        run_id, acceptance_backend.expected_output(run_id, COMPLETE_SPEC["steps"])
    )
    if item["status"] != "finished":
        reason = receipt.get("reason") if isinstance(receipt, dict) else None
        return _check(FAIL, f"item ended {item['status']}" + (f": {reason}" if reason else ""))
    if receipt.get("backend_status") != "success" or receipt.get("text") != expected:
        return _check(FAIL, "receipt output does not match the job spec digest")
    return _check(PASS, "finished; output digest verified")


def _cancel_check(
    item: dict[str, Any] | None, evts: list[dict[str, Any]], run_id: str
) -> dict[str, str]:
    if item is None:
        return _check(FAIL, "the cancel item is gone from the queue")
    if not _terminal(item):
        state = "cancel requested" if item.get("cancel_requested") else "running, no cancel yet"
        return _check(PENDING, f"item is {item['status']} ({state})")
    receipt = item.get("receipt") or {}
    relay_indices = [i for i, e in enumerate(evts) if e["kind"] == "hub_cancel_relayed"]
    launches = [
        i
        for i, e in enumerate(evts)
        if e["kind"] == "acceptance_launch" and e["data"].get("run") == run_id
    ]
    progress = [
        i
        for i, e in enumerate(evts)
        if e["kind"] == "acceptance_progress"
        and e["data"].get("run") == run_id
        and e["data"].get("step") == 1
    ]
    stopped = [
        e
        for i, e in enumerate(evts)
        if e["kind"] == "acceptance_stopped"
        and e["data"].get("run") == run_id
        and e["data"].get("confirmed") is True
        and any(
            launch < step < relay < i
            for launch in launches
            for step in progress
            for relay in relay_indices
        )
    ]
    if (
        item["status"] == "killed"
        and item.get("cancel_requested")
        and receipt.get("reason") == "cancelled_by_hub"
        and stopped
    ):
        return _check(PASS, f"stopped mid-run at step {stopped[-1]['data'].get('at_step')}")
    if item["status"] == "cancelled":
        return _check(FAIL, "cancelled before the device ever ran it")
    reason = receipt.get("reason") if isinstance(receipt, dict) else None
    return _check(
        FAIL,
        f"item ended {item['status']}"
        + (f" ({reason})" if reason else "")
        + ("" if stopped else "; no confirmed stop event"),
    )


def revocation_probe(node: str) -> dict[str, str]:
    """Prove the ownership gate refuses this registration once revoked. Writes nothing.

    Runs ``node_registry._owned_node``, the first check of every claim,
    heartbeat and receipt, against the stored row. Each probe must answer 403.
    """
    conn = storage.connect()
    try:
        row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (node,)).fetchone())
        if row is None:
            return _check(FAIL, "node is not registered")
        refused: list[str] = []
        admitted: list[str] = []

        def expect_refusal(label: str, probe: Any) -> None:
            try:
                probe()
            except NodeError as exc:
                (refused if exc.status == 403 else admitted).append(label)
            else:
                admitted.append(label)

        expect_refusal(
            "foreign_agent",
            lambda: node_registry._owned_node(conn, node, PROBE_IDENTITY, row.get("device_node")),
        )
        if row.get("device_node"):
            expect_refusal(
                "foreign_device",
                lambda: node_registry._owned_node(conn, node, row["agent"], PROBE_DEVICE),
            )
        expect_refusal(
            "revoked_agent",
            lambda: node_registry._check_binding(
                row, row["agent"], row.get("device_node"), revoked=frozenset({row["agent"]})
            ),
        )
    finally:
        conn.close()
    if admitted:
        return _check(FAIL, "claim gate admitted: " + ", ".join(admitted))
    return _check(PASS, "claim refused (403) for: " + ", ".join(refused))


def _timed_out_reason(diagnostics_passed: bool) -> str:
    """Why a run past ``DEADLINE_S`` failed, naming the missing step.

    The deadline also covers operator attestation, so a run whose
    diagnostics all passed can still time out. Saying so keeps readers from
    chasing a device fault when only the attestation was missing (task #2171).
    """
    if diagnostics_passed:
        return (
            f"timed_out awaiting_operator_attestation: every diagnostic passed but no "
            f"operator attested within {DEADLINE_S}s; start a fresh run when an operator "
            "is ready to attest"
        )
    return f"timed_out after {DEADLINE_S}s; start a fresh run"


def _outcome(row: dict[str, Any], checks: dict[str, dict[str, str]], ref: datetime):
    if row["state"] == "failed":
        return "failed", row.get("reason")
    if row["state"] == "accepted":
        if not row.get("attested_by") or not row.get("attestation_ref"):
            return (
                "failed",
                "legacy_unattested: start diagnostics and obtain independent operator attestation",
            )
        return "accepted", row.get("reason")
    started = _parse(row.get("started_at"))
    if started is not None and ref - started > timedelta(seconds=DEADLINE_S):
        # Judge by the last stored evaluation, not the checks recomputed now: a
        # stored pending row was evaluated inside the deadline, so its passing
        # checks mean only the attestation was late. A diagnostic that first
        # passes after the deadline is a late device, not a missing operator.
        stored = row.get("checks") or {}
        passed_in_time = all((stored.get(name) or {}).get("status") == PASS for name in CHECKS)
        return "failed", _timed_out_reason(passed_in_time)
    failed = [name for name in CHECKS if checks[name]["status"] == FAIL]
    if failed:
        first = failed[0]
        return "failed", f"{first}: {checks[first]['detail']}"
    if all(checks[name]["status"] == PASS for name in CHECKS):
        return (
            "pending",
            "awaiting_operator_attestation: diagnostics passed (node self-report); "
            "an independent lifecycle operator must attest this run before real work",
        )
    started = _parse(row.get("started_at"))
    if started is not None and ref - started > timedelta(seconds=DEADLINE_S):
        waiting = [name for name in CHECKS if checks[name]["status"] == PENDING]
        return "failed", f"timed_out: still waiting on {', '.join(waiting)} after {DEADLINE_S}s"
    return "pending", None


def evaluate(node: Any, *, persist: bool = True, now: datetime | None = None) -> dict[str, Any]:
    """Read the run's items, events and receipts; record and return the outcome.

    ``persist=False`` computes the same view without writing anything (status
    reads). With ``persist=True`` it also cancels the cancel item once it is
    running, should the event-time cancel (``on_node_events``) have been missed.
    """
    name = node_registry.validate_node_name(node)
    ref = _now(now)
    conn = storage.connect()
    try:
        row = _load_row(conn, name)
        node_row = _row(conn.execute("SELECT * FROM nodes WHERE node=?", (name,)).fetchone())
    finally:
        conn.close()
    if row is None:
        return _not_run(name)
    run_id = row["run_id"]
    complete = node_queue.get_item(row["complete_item_id"]) if row["complete_item_id"] else None
    cancel_item = node_queue.get_item(row["cancel_item_id"]) if row["cancel_item_id"] else None
    complete_events = _item_events(row["complete_item_id"])
    cancel_events = _item_events(row["cancel_item_id"])
    if (
        persist
        and cancel_item is not None
        and cancel_item.get("status") == "claimed"
        and not cancel_item.get("cancel_requested")
        and _of_run(cancel_events, "acceptance_progress", run_id)
    ):
        node_queue.cancel(cancel_item["id"], actor=ACTOR)
        cancel_item = node_queue.get_item(cancel_item["id"])
    launch, progress = _launch_and_progress(complete, complete_events, run_id)
    checks = {
        "launch": launch,
        "progress": progress,
        "receipt": _receipt_check(complete, run_id),
        "cancel_confirmed": _cancel_check(cancel_item, cancel_events, run_id),
        "revocation": revocation_probe(name)
        if node_row is not None
        else _check(FAIL, "node is no longer registered"),
    }
    if node_row is not None and (
        (config._node_hosted_or_public() and not node_row.get("device_node"))
        or node_row["agent"] != row["agent"]
        or node_row.get("device_node") != row.get("device_node")
    ):
        state, reason = (
            "failed",
            "node registration binding no longer matches this run; re-register and start a fresh run",
        )
    else:
        state, reason = _outcome(row, checks, ref)
        if state == "pending" and row["state"] != "pending":
            # An ended run never reads pending again: a timed-out run stays failed
            # (until a new start), however its unfinished items look later.
            state, reason = row["state"], row.get("reason")
    facts = _load((node_row or {}).get("facts_json")) or {}
    code_sha = node_registry.bounded_code_sha(facts.get("code_sha")) or row.get("code_sha")
    as_of = _iso(ref)
    accepted_at = row.get("accepted_at") or (as_of if state == "accepted" else None)
    if state != "accepted":
        accepted_at = None
    result = {
        **row,
        "state": state,
        "reason": reason,
        "checks": checks,
        "code_sha": code_sha,
        "evaluated_at": as_of,
        "accepted_at": accepted_at,
    }
    if persist:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_acceptance SET state=?, checks_json=?, reason=?, code_sha=?, "
                "evaluated_at=?, accepted_at=?, updated_at=? WHERE node=? AND run_id=? AND state=?",
                (
                    state,
                    json.dumps(checks),
                    reason,
                    code_sha,
                    as_of,
                    accepted_at,
                    as_of,
                    name,
                    run_id,
                    row["state"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        if state == "failed":
            _cancel_run_items(name, run_id)
        if state != row["state"]:
            _record(
                "acceptance_" + state,
                {"node": name, "run_id": run_id, "reason": reason, "code_sha": code_sha},
                ACTOR,
            )
    return _public(result)


def _not_run(name: str) -> dict[str, Any]:
    return {
        "node": name,
        "state": "not_run",
        "reason": "no acceptance run yet",
        "checks": {},
        "required": config.node_require_acceptance(),
    }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "node": row["node"],
        "run_id": row["run_id"],
        "agent": row["agent"],
        "state": row["state"],
        "reason": row.get("reason"),
        "checks": row.get("checks") or {},
        "complete_item_id": row.get("complete_item_id"),
        "cancel_item_id": row.get("cancel_item_id"),
        "code_sha": row.get("code_sha"),
        "started_by": row.get("started_by"),
        "started_at": row.get("started_at"),
        "evaluated_at": row.get("evaluated_at"),
        "accepted_at": row.get("accepted_at"),
        "required": config.node_require_acceptance(),
        "diagnostics_passed": all(
            (row.get("checks") or {}).get(name, {}).get("status") == PASS for name in CHECKS
        ),
        "evidence_trust": "node_self_report",
        "attested_by": row.get("attested_by"),
        "attestation_ref": row.get("attestation_ref"),
    }


def get(node: Any) -> dict[str, Any] | None:
    """The stored row as last evaluated, or None. Read-only."""
    name = node_registry.validate_node_name(node)
    conn = storage.connect()
    try:
        row = _load_row(conn, name)
    finally:
        conn.close()
    return _public(row) if row is not None else None


def _summary(row: dict[str, Any], ref: datetime) -> dict[str, Any]:
    state, reason = row["state"], row.get("reason")
    if "registration_agent" in row and (
        row["registration_agent"] != row["agent"]
        or row.get("registration_device_node") != row.get("device_node")
        or (config._node_hosted_or_public() and not row.get("registration_device_node"))
    ):
        state, reason = "failed", "node registration binding changed; start a fresh run"
    started = _parse(row.get("started_at"))
    if state == "pending" and started is not None and ref - started > timedelta(seconds=DEADLINE_S):
        passed = all((row["checks"].get(name) or {}).get("status") == PASS for name in CHECKS)
        state, reason = "failed", _timed_out_reason(passed)
    if state == "accepted" and not row.get("attested_by"):
        state, reason = "failed", "legacy_unattested: start a fresh independently attested run"
    return {
        "state": state,
        "agent": row["agent"],
        "run_id": row["run_id"],
        "reason": reason,
        "checks": {name: (row["checks"].get(name) or {}).get("status") for name in CHECKS},
        "code_sha": row.get("code_sha"),
        "started_at": row.get("started_at"),
        "evaluated_at": row.get("evaluated_at"),
        "accepted_at": row.get("accepted_at"),
    }


def read_states(*, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Stored acceptance summary per node, for listings. Raises ``sqlite3.Error``.

    Cheap: no event reads. A run pending past its deadline reads ``failed``.
    """
    ref = _now(now)
    conn = storage.connect()
    try:
        try:
            rows = conn.execute(
                "SELECT a.*, n.agent AS registration_agent, "
                "n.device_node AS registration_device_node FROM node_acceptance a "
                # Task #2181: a retired node is not in the fleet, so its last
                # run is not listed (the stored row is kept).
                "LEFT JOIN nodes n ON n.node=a.node WHERE n.retired_at IS NULL"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise
    finally:
        conn.close()
    out = {}
    for raw in rows:
        row = _row(raw)
        row["checks"] = _load(row.pop("checks_json")) or {}
        out[row["node"]] = _summary(row, ref)
    return out


def states() -> dict[str, dict[str, Any]]:
    """``read_states`` that never raises (an unreadable store lists nothing)."""
    try:
        return read_states()
    except sqlite3.Error:
        return {}


def _cancel_run_items(node: str, run_id: str) -> None:
    conn = storage.connect()
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM node_dispatch WHERE node=? AND backend=? "
                "AND json_valid(prompt) AND json_extract(prompt, '$.run')=? "
                "AND status IN ('queued', 'claimed')",
                (node, BACKEND, run_id),
            ).fetchall()
        ]
    finally:
        conn.close()
    for item_id in ids:
        node_queue.cancel(item_id, actor=ACTOR)


def expire_pending(*, now: datetime | None = None) -> None:
    """Periodic maintenance independent of scheduler and device availability."""
    conn = storage.connect()
    try:
        ensure_schema(conn)
        names = [
            r["node"]
            for r in conn.execute(
                "SELECT node FROM node_acceptance a WHERE state='pending' "
                "OR (state='failed' AND EXISTS (SELECT 1 FROM node_dispatch d "
                "WHERE d.id IN (a.complete_item_id, a.cancel_item_id) "
                "AND d.status IN ('queued', 'claimed') AND d.cancel_requested=0)) "
                "ORDER BY started_at LIMIT 100"
            ).fetchall()
        ]
    finally:
        conn.close()
    for name in names:
        evaluate(name, now=now)


def attest(node: str, run_id: str, evidence_ref: str, *, actor: str) -> dict[str, Any]:
    """Independent operator attestation of this exact run, never node evidence.

    An operator verifies the device execution/output and cancellation outside
    its self-reported API. This records that human trust decision; it cannot
    establish the truth of a dishonest operator's assertion.
    """
    from . import agent_lifecycle

    if actor not in agent_lifecycle.operators():
        raise NodeError("only an independent lifecycle operator may attest device acceptance", 403)
    if not isinstance(evidence_ref, str) or not 1 <= len(evidence_ref.strip()) <= 1000:
        raise NodeError("evidence_ref must identify independent verification (1-1000 chars)", 400)
    name = node_registry.validate_node_name(node)
    evaluate(name)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _load_row(conn, name)
        node_row = _node_row(conn, name)
        if actor == node_row["agent"]:
            raise NodeError("a node's own agent cannot attest its acceptance", 403)
        if row is None or row["run_id"] != run_id or row["state"] != "pending":
            raise NodeError("attestation requires the exact pending acceptance run", 409)
        if (
            node_row["agent"] != row["agent"]
            or node_row.get("device_node") != row.get("device_node")
        ) or not all(row["checks"].get(name, {}).get("status") == PASS for name in CHECKS):
            raise NodeError("all transport diagnostics must pass before attestation", 409)
        now = datetime.now(UTC)
        if now - _parse(row["started_at"]) > timedelta(seconds=DEADLINE_S):
            raise NodeError("acceptance run timed out; start a fresh run", 409)
        ts = _iso(now)
        conn.execute(
            "UPDATE node_acceptance SET state='accepted', reason=NULL, accepted_at=?, "
            "attested_by=?, attestation_ref=?, updated_at=? WHERE node=? AND run_id=?",
            (ts, actor, evidence_ref.strip(), ts, name, run_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _record(
        "acceptance_attested",
        {
            "node": name,
            "run_id": run_id,
            "attested_by": actor,
            "evidence_ref": evidence_ref.strip(),
        },
        actor,
    )
    return get(name) or {}


def claim_filter(conn: sqlite3.Connection, node_row: dict[str, Any], ref: datetime):
    """Transaction-local admission; diagnostics must belong to the live run.

    Held real jobs stay queued, and never block diagnostics behind them.
    """
    row = _load_row(conn, node_row["node"])
    accepted = _accepted(row, node_row["agent"], node_row.get("device_node"))
    live = (
        row is not None
        and row["state"] == "pending"
        and row["agent"] == node_row["agent"]
        and row.get("device_node") == node_row.get("device_node")
    )
    if live:
        started = _parse(row.get("started_at"))
        live = started is not None and ref - started <= timedelta(seconds=DEADLINE_S)
    ids = [row.get("complete_item_id"), row.get("cancel_item_id")] if live else [None, None]
    real = accepted or not config.node_require_acceptance()
    return "AND ((backend!='acceptance' AND ?) OR (backend='acceptance' AND id IN (?, ?))) ", [
        int(real),
        *ids,
    ]


# --- the gate (UA_NODE_REQUIRE_ACCEPTANCE) --------------------------------------


def _accepted(row: dict[str, Any] | None, agent: Any, device_node: str | None = None) -> bool:
    return (
        row is not None
        and row["state"] == "accepted"
        and row["agent"] == agent
        and row.get("device_node") == device_node
        and (not config._node_hosted_or_public() or bool(device_node))
        and bool(row.get("attested_by"))
        and bool(row.get("attestation_ref"))
    )


def require_accepted(conn: sqlite3.Connection, node_row: dict[str, Any], backend: str) -> None:
    """Refuse non-acceptance work for a node that has not passed acceptance."""
    if backend == BACKEND or not config.node_require_acceptance():
        return
    name = node_row["node"]
    row = _load_row(conn, name)
    if _accepted(row, node_row["agent"], node_row.get("device_node")):
        return
    state = "never run" if row is None else row["state"]
    raise NodeError(
        f"node {name!r} has not passed device acceptance ({state}); "
        "UA_NODE_REQUIRE_ACCEPTANCE=1 holds real work until it does. Start it with "
        f"coordination_manage(action='node_acceptance_start', params={{'node': '{name}'}}) "
        "or POST /api/action/node-acceptance, then retry",
        status=409,
    )


def filter_accepted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The node rows whose latest acceptance run is accepted for their agent."""
    conn = storage.connect()
    try:
        return [
            row
            for row in rows
            if _accepted(_load_row(conn, row["node"]), row["agent"], row.get("device_node"))
        ]
    finally:
        conn.close()


# --- node_queue hooks ------------------------------------------------------------


def on_node_events(item: dict[str, Any], kinds: list[str]) -> None:
    """Cancel the run's cancel item once it reports progress. Never raises."""
    try:
        if item.get("backend") != BACKEND or "acceptance_progress" not in kinds:
            return
        conn = storage.connect()
        try:
            row = _load_row(conn, item["node"])
        finally:
            conn.close()
        if row is None or row["cancel_item_id"] != item["id"] or row["state"] != "pending":
            return
        fresh = node_queue.get_item(item["id"])
        if fresh is not None and fresh["status"] == "claimed" and not fresh["cancel_requested"]:
            node_queue.cancel(item["id"], actor=ACTOR)
    except Exception:  # noqa: BLE001 - acceptance bookkeeping never fails a node call
        _log.warning("acceptance cancel hook failed for %s", item.get("id"), exc_info=True)


def on_receipt(item: dict[str, Any]) -> None:
    """Re-evaluate the node's run after an acceptance receipt. Never raises."""
    try:
        if item.get("backend") != BACKEND:
            return
        conn = storage.connect()
        try:
            row = _load_row(conn, item["node"])
        finally:
            conn.close()
        if row is not None and item["id"] in (row["complete_item_id"], row["cancel_item_id"]):
            evaluate(item["node"])
    except Exception:  # noqa: BLE001 - acceptance bookkeeping never fails a node call
        _log.warning("acceptance evaluation failed for %s", item.get("node"), exc_info=True)


# --- MCP actions (coordination_manage) -------------------------------------------


def _tool_call(fn: Any) -> dict[str, Any]:
    try:
        return fn()
    except NodeError as exc:
        raise ToolError(str(exc)) from exc


def node_acceptance_start(node: str) -> dict:
    """Queue a device acceptance run on ``node`` (its agent or an operator only)."""
    actor = config.verified_agent_identity()

    def run() -> dict[str, Any]:
        authorize(actor, node)
        return start(node, actor=str(actor))

    return _tool_call(run)


def node_acceptance_evaluate(node: str) -> dict:
    """Re-evaluate ``node``'s latest acceptance run and record the outcome."""
    actor = config.verified_agent_identity()

    def run() -> dict[str, Any]:
        authorize(actor, node)
        return evaluate(node)

    return _tool_call(run)


def node_acceptance_status(node: str | None = None) -> dict:
    """Read acceptance: one node's live evaluation, or every node's stored summary."""
    if node is None:
        return {"nodes": states(), "required": config.node_require_acceptance()}
    return _tool_call(lambda: evaluate(node, persist=False))


def node_acceptance_attest(node: str, run_id: str, evidence_ref: str) -> dict:
    """Attest independently verified execution of one exact diagnostic run (operator only)."""
    return _tool_call(
        lambda: attest(
            node, run_id, evidence_ref, actor=str(config.verified_agent_identity() or "")
        )
    )
