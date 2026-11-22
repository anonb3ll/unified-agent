"""Global approval queue for gated tool calls (task #62 · C).

A hub-side chokepoint at the same layer as the run_cmd allowlist: the hub
config (`UA_APPROVAL_TOOLS`, colon-separated tool names) decides which tools
require human approval — agents cannot opt themselves in or out (the operator's
2026-07-17 decision: global allowlist, no agent self-reporting).

BASELINE PLUS OPT-IN: `UA_APPROVAL_TOOLS` adds tools to the gate, and is unset
by default. The gated set always includes the baseline `{"delegate"}` unless
`UA_APPROVAL_BASELINE=0` (or `UA_LEGACY_OPEN_POSTURE=1`); see
`config.approval_gated_tools`. So with `UA_APPROVAL_TOOLS` unset, `delegate`
parks agent submissions; verified human web submissions already carry authority
and skip that gate. Other tools keep their existing behavior, including forced
approval for work-product release and production actions.

Batching instead of one-shot blocking: a gated call parks a 'pending' row and
raises; many requests can accumulate and be reviewed together via
`approval_pending()` / `approval_resolve()`. An 'approved' row is a one-shot
pass for that exact (tool, payload) invocation — the retried call consumes it
and runs. The gate runs AFTER a tool's own safety checks (allowlist, sandbox,
path resolution), never instead of them.

A pass is time-bounded in both directions (task #171): a human can revoke an
approved-but-unconsumed pass, and one that is never consumed expires on its own
after `UA_APPROVAL_PASS_TTL_HOURS` (default 24). Without those, an unconsumed
approval was a permanent standing grant — two passes approved 2026-07-19 were
still live a week later, inert only because UA_APPROVAL_TOOLS happened to be
empty at the time. Neither state deletes the row: 'revoked' and 'expired' stay
in the audit trail, and both are distinct from 'denied' because the human never
refused the action — a later identical request re-queues for a fresh decision.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import approval_push, config, edition, events, storage
from ..tool_errors import TypedToolError

logger = logging.getLogger(__name__)

# A producer stamp that raised (task #1640). The approval still stands; this
# event makes the missing attribution visible instead of silent.
CHANNEL_EVIDENCE_GAP = "audit.evidence.gap"

# Approving any queued call requires a different human (the web route's rule).
# Denying or revoking stays open to the requester — withdrawing is not
# escalation. This set names the actions that were first held to that bar
# (task #1278 and follow-ons); approval_resolve now applies it to every tool.
SEPARATION_OF_DUTIES_TOOLS = frozenset(
    {
        "work_product_release",
        "acp_permission",
        "harness_config_apply",
        # Config rollback and approval-rule edits (task #1352): the requester
        # cannot approve their own restore or their own change to require_approval.
        "config_revision_rollback",
        "config_revision_commit",
        "org_proposal_apply",
    }
)

# Evidence tier (task #1672, H12). Approvals carry no risk tier of their own;
# the separation-of-duties set above is the one place the code already names
# the actions held to a higher bar, so it is the high tier here. A high-tier
# approve refuses to commit without its signed producer stamp; every other
# resolution proceeds and records an ``audit.evidence.gap``.
HIGH_TIER_EVIDENCE_TOOLS = SEPARATION_OF_DUTIES_TOOLS

# error_type recorded when the stamp returned None instead of raising.
NO_PRODUCER_STAMP = "NoProducerStamp"

# The Approval Ledger's GitHub tool (``github_app.TOOL_NAME``; that module sits
# above this gate, so the name is restated). Approving or denying one of its
# rows hands the head to ``github_app_fastpath`` for a prompt status (#2234).
LEDGER_TOOL = "github_app.process_pr_event"

# The ledger fast path (#2234) lives in ``github_app_fastpath`` (github), which
# this kernel gate may not import. It registers itself here when imported, and
# ``post_init.register_default_hooks`` imports it in every hub process (#2450).
# Absent, a resolution logs that the scheduled poll will publish the status:
# the fast path was always best-effort, with the poll as its backstop.
_ledger_fast_path: Callable[[int, str, str], Any] | None = None


def register_ledger_fast_path(handler: Callable[[int, str, str], Any]) -> None:
    """Install ``handler(approval_id, tool, status)``, called after a ledger row resolves."""
    global _ledger_fast_path
    if not callable(handler):
        raise TypeError("ledger fast path must be callable")
    _ledger_fast_path = handler


def ledger_fast_path() -> Callable[[int, str, str], Any] | None:
    """The registered ledger fast path, or None in a process that never loaded it."""
    return _ledger_fast_path


def evidence_tier(tool: str | None) -> str:
    """``"high"`` when an approval of ``tool`` must carry a producer stamp, else ``"low"``."""
    return "high" if tool in HIGH_TIER_EVIDENCE_TOOLS else "low"


class ApprovalPendingError(TypedToolError):
    """Raised when a gated call has been parked in the approval queue.

    A distinct subclass so programmatic callers (workflow_run) can tell
    "waiting on a human" apart from a genuine failure and skip retries.
    ``approval_id`` is set when the queue row is known so HTTP/UI callers can
    surface the pending pass without parsing the message.
    """

    code = "approval_required"

    def __init__(self, message: str, *, approval_id: int | None = None):
        super().__init__(message, approval_id=approval_id)
        self.approval_id = approval_id


def _row(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    d["payload"] = json.loads(d.get("payload") or "{}")
    return d


def _canonical_payload(payload: dict | None) -> str:
    """Stable JSON for exact-match pairing of a request with its approval."""
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def canonical_approval_payload(payload: dict | None) -> str:
    """Public accessor for the approvals-owned canonical payload form."""
    return _canonical_payload(payload)


def supplied_arguments_payload(arguments: dict[str, object]) -> dict[str, object]:
    """The approval payload for one tool call: its supplied arguments only.

    ``None`` means omitted, so an MCP call (which passes every parameter, with
    its default) and an HTTP read naming the same arguments queue and consume
    the same request. The registered wrapper's generic gate and the governed
    HTTP reads both build their payload here (task #1811).
    """
    return {key: value for key, value in arguments.items() if value is not None}


_native_execution_gate: ContextVar[str | None] = ContextVar("native_execution_gate", default=None)


_invocation_passes: ContextVar[dict | None] = ContextVar("approval_invocation_passes", default=None)
_EXECUTION_STARTED = "_execution_started"
_PRE_EXECUTION_FAILURE = "_pre_execution_failure"
_BOUND_ACTION = "_bound_action"
_JOIN_AVAILABLE = "_join_available"
_CLOSED = "_closed"
_BOUND_CACHE = "_bound_cache"
# A copied context sees its originating lease close. A child that joined while
# live gets its own lease, so an in-flight worker can finish after owner exit.
_invocation_lease: ContextVar[dict | None] = ContextVar("approval_invocation_lease", default=None)


def _receipt_key(tool: str, payload: dict | None, requested_by: str | None) -> tuple:
    identity = config.verified_agent_identity()
    # A claimed requester label must never impersonate a verified identity.
    return (tool, _canonical_payload(payload), (identity, None if identity else requested_by))


def adopt_invocation_receipt(
    tool: str, payload: dict | None, requested_by: str | None, receipt: dict
) -> None:
    """Bind trusted ledger evidence as this invocation's receipt for ``tool``.

    Only for a receipt read from durable evidence, never one a caller supplied.
    It stays subject to ``validate_receipt``. Call inside ``approval_invocation``.
    """
    lease = _invocation_lease.get()
    if lease is None or not lease["active"]:
        raise RuntimeError("no active approval invocation to adopt a receipt into")
    if "_consumption_nonce" not in receipt:
        # The gate validates a cached receipt only when it carries the nonce.
        raise ToolError("only a consumed approval receipt can be adopted")
    lease["cache"][_receipt_key(tool, payload, requested_by)] = receipt


@contextmanager
def approval_invocation(
    tool: str | None = None,
    payload: dict | None = None,
    *,
    requested_by: str | None = None,
    join: bool = False,
    join_existing: bool = False,
    restore_unstarted: bool = False,
):
    """Share a consumed pass between gates protecting ONE tool invocation.

    No-argument callers always get an isolated scope. An explicit binding
    permits one child with join=True to use the enclosing receipts only for
    the same tool, canonical payload and identity. Execution closes the join
    unless its caller explicitly hands off to that child. If a governed wrapper
    rejects before entering the bare tool, restore any consumed pass so the
    denial did not spend approval; once execution starts, the pass stays spent.
    ``join_existing`` is internal plumbing for a boundary participating in the
    same not-yet-started action. An already executing parent gets a separate
    scope. ``restore_unstarted`` also restores a structured pause/dry-run that
    returns normally without execution.
    """
    existing = _invocation_passes.get()
    if join_existing and existing is not None and not existing.get(_EXECUTION_STARTED):
        lease = _invocation_lease.get()
        cache = lease["cache"] if lease is not None and lease["active"] else {}
        child_lease = {"active": True, "cache": cache}
        lease_token = _invocation_lease.set(child_lease)
        try:
            yield existing
        finally:
            if restore_unstarted and not existing.get(_EXECUTION_STARTED):
                _restore_unstarted_receipts(existing)
            child_lease["active"] = False
            _invocation_lease.reset(lease_token)
        return
    binding = _receipt_key(tool, payload, requested_by) if tool is not None else None
    parent = _invocation_passes.get()
    if (
        join
        and binding is not None
        and parent is not None
        and not parent.get(_CLOSED)
        and parent.get(_BOUND_ACTION) == binding
        and parent.pop(_JOIN_AVAILABLE, False)
    ):
        receipts = parent
    else:
        receipts = {_JOIN_AVAILABLE: False}
        if binding is not None:
            receipts[_BOUND_ACTION] = binding
            receipts[_JOIN_AVAILABLE] = True
    joined = receipts is parent
    cache = dict(receipts.get(_BOUND_CACHE, {})) if joined else {}
    if joined:
        receipts.get(_BOUND_CACHE, {}).clear()
    lease = {"active": True, "cache": cache}
    if not joined:
        receipts[_BOUND_CACHE] = cache
    token = _invocation_passes.set(receipts)
    lease_token = _invocation_lease.set(lease)
    try:
        yield receipts
        if not joined and receipts.get(_EXECUTION_STARTED):
            _record_call_outcomes(receipts, "completed")
    except Exception:
        if not receipts.get(_EXECUTION_STARTED):
            receipts[_PRE_EXECUTION_FAILURE] = True
            for receipt in tuple(receipts.values()):
                if isinstance(receipt, dict) and receipt.get("approval_id") is not None:
                    _restore_receipt(receipt)
        elif not joined:
            _record_call_outcomes(receipts, "failed")
        raise
    finally:
        lease["active"] = False
        cache.clear()
        receipts[_JOIN_AVAILABLE] = False
        receipts.get(_BOUND_CACHE, {}).clear()
        if not joined:
            receipts[_CLOSED] = True
        if restore_unstarted and not receipts.get(_EXECUTION_STARTED):
            _restore_unstarted_receipts(receipts)
        _invocation_lease.reset(lease_token)
        _invocation_passes.reset(token)


def _restore_unstarted_receipts(receipts: dict) -> None:
    receipts[_PRE_EXECUTION_FAILURE] = True
    for receipt in tuple(receipts.values()):
        if isinstance(receipt, dict) and receipt.get("approval_id") is not None:
            _restore_receipt(receipt)


def governed_execution_started() -> bool:
    """Whether the current joined action crossed its execution boundary."""
    receipts = _invocation_passes.get()
    return bool(receipts and receipts.get(_EXECUTION_STARTED))


#: Observers of the execution boundary (task #2516): ``execution_probe`` installs
#: one, and ``mark_governed_execution`` records that a call it encloses crossed
#: the boundary. A copied context (a worker thread) shares the same dict.
_execution_probe: ContextVar[dict | None] = ContextVar("approval_execution_probe", default=None)


@contextmanager
def execution_probe():
    """Observe whether any governed call inside this scope began executing.

    Yields ``{"started": bool}``. ``started`` turns True at the first
    ``mark_governed_execution`` (the boundary every wrapper path crosses before
    a tool's effects: the generic and manager wrappers, native self-gated tools
    once their own gate passes, merges, work products). While it stays False,
    every failure in the scope was a refusal before any effect: the profile
    ceiling, agent access, policy, the approval gate or a native gate's park. A
    keyed write uses it to classify a failure by phase, not by status code.
    """
    probe = {"started": False}
    token = _execution_probe.set(probe)
    try:
        yield probe
    finally:
        _execution_probe.reset(token)


def mark_probe_started() -> None:
    """Record the execution boundary for a write that runs outside a tool wrapper."""
    probe = _execution_probe.get()
    if probe is not None:
        probe["started"] = True


def mark_governed_execution(*, handoff: bool = False) -> None:
    """Spend receipts at execution, optionally retaining one bound child join.

    Native adapters mark before entering the registered wrapper. That wrapper
    may claim the handoff once; marking again never rearms a spent join.
    """
    mark_probe_started()
    receipts = _invocation_passes.get()
    if receipts is not None:
        if not handoff or receipts.get(_EXECUTION_STARTED):
            receipts[_JOIN_AVAILABLE] = False
            receipts.get(_BOUND_CACHE, {}).clear()
            lease = _invocation_lease.get()
            if lease is not None and lease.get("active"):
                lease.get("cache", {}).clear()
        receipts[_EXECUTION_STARTED] = True


@contextmanager
def native_approval_execution(tool: str):
    """Known self-gated natives cross execution only after their own gate passes.

    Their safety checks and native approval payload precede effects. An unknown
    tool still spends conservatively at bare entry; it cannot use this seam.
    """
    token = _native_execution_gate.set(tool)
    try:
        yield
    finally:
        _native_execution_gate.reset(token)


def failed_before_execution(receipts: dict | None) -> bool:
    """Whether an approval invocation failed before entering its bare tool."""
    return bool(receipts and receipts.get(_PRE_EXECUTION_FAILURE))


_GRANT_FIELDS = ("requested_by", "resolved_by", "resolved_at", "created_at")
_CONSUMPTION_CHANNEL = "agent.approval.consumed"
#: Hash-chained record of every approve / deny / revoke (task #1909).
CHANNEL_APPROVAL_RESOLVED = "agent.approval.resolved"


def _latest_consumption(conn, approval_id):
    row = conn.execute(
        "SELECT ulid FROM event_log WHERE channel=? "
        "AND json_extract(payload, '$.approval_id')=? ORDER BY ROWID DESC LIMIT 1",
        (_CONSUMPTION_CHANNEL, approval_id),
    ).fetchone()
    return row["ulid"] if row else None


def _approval_principal() -> dict:
    actor = config.verified_agent_identity()
    metadata = config.current_auth_metadata() or {}
    subject = (
        "workos:" + str(metadata.get("workos_user_id"))
        if config.workos_request_is_bound()
        else ("agent:" + actor if actor else None)
    )
    return {"org_id": _tenant(), "actor": actor, "subject": subject}


def validate_receipt(conn, key, receipt, *, require_live: bool = True):
    """Public port to ``_validate_receipt`` for a boundary that re-checks a held receipt."""
    return _validate_receipt(conn, key, receipt, require_live=require_live)


def _validate_receipt(conn, key, receipt, *, require_live: bool = True):
    row = conn.execute(
        "SELECT * FROM approvals WHERE id=? AND org_id IS ?", (receipt["approval_id"], _tenant())
    ).fetchone()
    if row is None or row["status"] != "consumed":
        status = row["status"] if row else "missing"
        raise ToolError(
            f"approval request {receipt['approval_id']} is {status}; fresh approval required"
        )
    if "_principal" in receipt and receipt["_principal"] != _approval_principal():
        raise ToolError("approval consumption belongs to a different current principal")
    expires = _pass_expires_at(row)
    if require_live and (expires is None or datetime.now(UTC) >= expires):
        raise ToolError(f"approval request {row['id']} expired before execution")
    if (row["tool"], row["payload"]) != key[:2] or any(
        row[field] != receipt["_grant"][field] for field in _GRANT_FIELDS
    ):
        raise ToolError(f"approval request {row['id']} grant changed before execution")
    if _latest_consumption(conn, row["id"]) != receipt["_consumption_nonce"]:
        raise ToolError(f"approval request {row['id']} consumption was superseded")


_completed_consumption_read: ContextVar[bool] = ContextVar(
    "completed_consumption_read", default=False
)


@contextmanager
def completed_consumption_read():
    """Trusted native composition reading a completed result, never executing."""
    token = _completed_consumption_read.set(True)
    try:
        yield
    finally:
        _completed_consumption_read.reset(token)


def revalidate_invocation_approvals(conn) -> None:
    """Verify live grants and this invocation's exact consumption under its fence.

    The supplied canonical connection owns the boundary transaction. No second
    pass is consumed. A receipt's unique consumption event prevents restore /
    reconsume ABA from authorizing the older invocation or restoring its pass.
    """
    for receipt in (_invocation_passes.get() or {}).values():
        if (
            isinstance(receipt, dict)
            and "_grant" in receipt
            and "_consumption_nonce" in receipt
            and not receipt.get("_unused_tool")
        ):
            _validate_receipt(conn, (receipt.get("tool"), receipt.get("payload")), receipt)


CHANNEL_TASK_RECEIPT = "agent.task.invocation_receipt"
CHANNEL_TASK_COMMITTED = "agent.task.invocation_committed"
_COMMITTED_TASK_REPLAY = "_committed_task_replay"
_task_replay_binding: ContextVar[dict | None] = ContextVar("task_replay_binding", default=None)


_task_binding_ports: ContextVar[tuple | None] = ContextVar("task_binding_ports", default=None)


@contextmanager
def selected_task_binding(binding, semantic_digest, committed_receipt):
    """Trusted composition supplies authority/normalization ports for one call.

    This core neither imports the task service nor discovers request authority.
    The registered selected writer installs these ports for its actual call.
    """
    token = _task_binding_ports.set((binding, semantic_digest, committed_receipt))
    try:
        yield
    finally:
        _task_binding_ports.reset(token)


def registered_task_binding() -> dict | None:
    ports = _task_binding_ports.get()
    return ports[0]() if ports is not None else None


def _task_semantic_digest(arguments: dict) -> str:
    ports = _task_binding_ports.get()
    if ports is None:
        raise ToolError("trusted selected-task normalization is required")
    return ports[1](arguments)


_SCOPED_TASK_RECEIPT_TOOL = "task_update.invocation"


def _task_receipt_storage_key(binding: dict) -> bytes:
    # Public client keys and their compatibility aliases are SQLite TEXT.
    # Bound bytes remain BLOB even in this table's TEXT-affinity key column,
    # so no caller-crafted string can occupy the internal namespace. The
    # digest still includes tenant, subject, actor, operation/version and key.
    return b"task-invocation:\0v2:" + binding["scope_key"].encode("ascii")


def _scoped_task_receipt(conn, binding: dict) -> dict | None:
    row = conn.execute(
        "SELECT tool, result_json FROM idempotency_keys WHERE key=?",
        (_task_receipt_storage_key(binding),),
    ).fetchone()
    if row is not None:
        if row["tool"] != _SCOPED_TASK_RECEIPT_TOOL:
            raise ToolError("internal task receipt has an unexpected record type")
        try:
            cached = json.loads(row["result_json"])
        except (TypeError, ValueError) as exc:
            raise ToolError("internal task receipt is invalid") from exc
        if not isinstance(cached, dict):
            raise ToolError("internal task receipt is invalid")
        return cached
    # Read-only compatibility with the published predecessor. The caller
    # still requires the original full producer binding/result proof below;
    # no receipt, principal or authority is inferred/backfilled here.
    return storage.get_idempotency_record(
        conn, "task-invocation:" + binding["scope_key"], _SCOPED_TASK_RECEIPT_TOOL
    )


def _record_scoped_task_receipt(conn, binding: dict, result: dict) -> None:
    key = _task_receipt_storage_key(binding)
    existing = conn.execute("SELECT tool FROM idempotency_keys WHERE key=?", (key,)).fetchone()
    if existing is not None and existing["tool"] != _SCOPED_TASK_RECEIPT_TOOL:
        raise ToolError("internal task receipt has an unexpected record type")
    # Required state belongs to the existing owner transaction. Do not route
    # this BLOB through the legacy TEXT tenant-prefix codec or commit here.
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, tool, result_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (key, _SCOPED_TASK_RECEIPT_TOOL, json.dumps(result, allow_nan=False), storage.now()),
    )


def record_task_idempotency_on_connection(conn, key: str, result: dict) -> None:
    """Use the existing store, with a trusted principal namespace for new calls."""
    binding = registered_task_binding()
    if binding is None:
        storage.record_idempotency_required(conn, key, "task_update", result)
        return
    _record_scoped_task_receipt(conn, binding, result)
    # Retain the original native alias for existing internal readers. Another
    # principal or tool must never replace that historical outcome.
    alias = conn.execute(
        "SELECT 1 FROM idempotency_keys WHERE key=?", (storage.idempotency_storage_key(key),)
    ).fetchone()
    if alias is None:
        storage.record_idempotency_required(conn, key, "task_update", result)


def bound_task_receipt(conn, key: str, task_id: int, *, allow_legacy: bool = True) -> dict | None:
    """Authenticate scoped receipts; legacy reads retain their ordinary gates.

    Historical unbound outcomes are not given inferred semantic/principal
    metadata. A producer-owned raw alias is never another principal's replay.
    Wrapper preflight excludes legacy reads so they cannot skip its gates.
    """
    binding = registered_task_binding()
    if binding is None:
        return storage.get_idempotency_record(conn, key, "task_update")
    if (
        conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (task_id, _tenant())
        ).fetchone()
        is None
    ):
        raise ToolError(f"no task with id {task_id}")
    cached = _scoped_task_receipt(conn, binding)
    row = conn.execute(
        "SELECT payload FROM event_log WHERE channel=? AND dedupe_key=? "
        "AND org_id IS ? AND originator=?",
        (CHANNEL_TASK_RECEIPT, "task-receipt:" + binding["scope_key"], _tenant(), binding["actor"]),
    ).fetchone()
    if row is None and cached is None:
        historical = storage.get_idempotency_record(conn, key, "task_update")
        if historical is None:
            return None
        encoded = json.dumps(historical, sort_keys=True, separators=(",", ":"), allow_nan=False)
        producer_owned = conn.execute(
            "SELECT 1 FROM event_log WHERE channel=? AND org_id IS ? "
            "AND json_extract(payload,'$.result_digest')=? LIMIT 1",
            (CHANNEL_TASK_RECEIPT, _tenant(), hashlib.sha256(encoded.encode()).hexdigest()),
        ).fetchone()
        if producer_owned is not None or not allow_legacy:
            return None
        if historical.get("id") != task_id or historical.get("org_id") != _tenant():
            raise ToolError("legacy task receipt does not match the authorized resource")
        return historical
    if row is not None and cached is None:
        # A predecessor producer-bound receipt used only the native alias.
        # Verify its original binding/result; do not backfill or rewrite it.
        cached = storage.get_idempotency_record(conn, key, "task_update")
    if cached is None or row is None:
        raise ToolError("task receipt has no matching committed request binding")
    persisted = json.loads(row["payload"])
    if persisted.get("binding") != binding:
        raise ToolError("task receipt is bound to a different semantic request")
    encoded = json.dumps(cached, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if (
        cached.get("id") != task_id
        or cached.get("org_id") != _tenant()
        or persisted.get("result_digest") != hashlib.sha256(encoded.encode()).hexdigest()
    ):
        raise ToolError("task receipt does not match its committed result")
    return cached


@contextmanager
def committed_task_replay():
    """Internal scope following actual resource and producer-receipt lookup."""
    binding = registered_task_binding()
    if binding is None:
        raise ToolError("a bound registered task receipt is required")
    token = _task_replay_binding.set(binding)
    try:
        yield
    finally:
        _task_replay_binding.reset(token)


def committed_task_replay_active() -> bool:
    """Whether this call was admitted only as a committed-receipt replay."""
    return bool(
        _task_replay_binding.get() or (_invocation_passes.get() or {}).get(_COMMITTED_TASK_REPLAY)
    )


def restore_unused_task_clear_on_connection(conn) -> None:
    """The writer did not clear quarantine; return only that exact native pass.

    This update participates in the selected owner's transaction. Generic
    authority for the actual task mutation stays consumed. On rollback the
    enclosing unstarted scope still restores the original durable consumption.
    """
    for item in (_invocation_passes.get() or {}).values():
        if (
            not isinstance(item, dict)
            or item.get("tool") != "task_untrusted_clear"
            or "_consumption_nonce" not in item
            or item.get("restored")
        ):
            continue
        _validate_receipt(conn, (item["tool"], item["payload"]), item)
        restored = conn.execute(
            "UPDATE approvals SET status='approved' WHERE id=? AND status='consumed' "
            "AND updated_at=? AND org_id IS ?",
            (item["approval_id"], item["generation"], _tenant()),
        )
        if restored.rowcount != 1:
            raise ToolError("unused quarantine approval changed before restoration")
        item["restored"] = True
        item["_unused_tool"] = True


def record_task_receipt_on_connection(conn, result: dict) -> None:
    """Prepare producer receipt and exact grant nonce references with mutation."""
    binding = registered_task_binding()
    revalidate_invocation_approvals(conn)
    grants = [
        {"approval_id": item["approval_id"], "nonce": item["_consumption_nonce"]}
        for item in (_invocation_passes.get() or {}).values()
        if isinstance(item, dict) and "_consumption_nonce" in item and not item.get("restored")
    ]
    if binding is None:
        if grants:
            # An unkeyed call has no replay/recovery identity, but committed
            # authority still needs durable protection from scope cleanup.
            events.record_event_once_on_connection(
                conn,
                CHANNEL_TASK_COMMITTED,
                payload={"task_id": result["id"], "approvals": grants},
                originator=config.verified_agent_identity(),
            )
        return
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    dedupe = "task-receipt:" + binding["scope_key"]
    if conn.execute("SELECT 1 FROM event_log WHERE dedupe_key=?", (dedupe,)).fetchone():
        raise ToolError("task receipt was already committed")
    events.record_event_once_on_connection(
        conn,
        CHANNEL_TASK_RECEIPT,
        payload={
            "binding": binding,
            "result_digest": hashlib.sha256(encoded.encode()).hexdigest(),
            "approvals": grants,
        },
        originator=binding["actor"],
        dedupe_key=dedupe,
    )


def _consumption_completed(conn, nonce: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM event_log e, json_each(e.payload, '$.approvals') a "
            "WHERE e.channel IN (?,?,?) AND e.org_id IS ? "
            "AND json_extract(a.value, '$.nonce')=? LIMIT 1",
            (
                CHANNEL_TASK_RECEIPT,
                CHANNEL_TASK_COMMITTED,
                "agent.synthetic_workflow.completed",
                _tenant(),
                nonce,
            ),
        ).fetchone()
        is not None
    )


def _recover_bound_consumption(conn, tool, canon, receipt_key, receipts, lease):
    return _recover_task_consumption(conn, tool, canon, receipt_key, receipts, lease)


def _recover_task_consumption(conn, tool, canon, receipt_key, receipts, lease):
    binding = registered_task_binding() if tool in {"task_update", "task_untrusted_clear"} else None
    if binding is None or receipts is None or lease is None or not lease["active"]:
        return None
    row = conn.execute(
        "SELECT ulid,payload FROM event_log WHERE channel=? AND org_id IS ? AND originator=? "
        "AND json_extract(payload,'$.binding.scope_key')=? "
        "AND json_extract(payload,'$.tool')=? ORDER BY rowid DESC LIMIT 1",
        (_CONSUMPTION_CHANNEL, _tenant(), binding["actor"], binding["scope_key"], tool),
    ).fetchone()
    if row is None:
        return None
    persisted = json.loads(row["payload"])
    approval = conn.execute(
        "SELECT * FROM approvals WHERE id=? AND org_id IS ?",
        (persisted.get("approval_id"), _tenant()),
    ).fetchone()
    if approval is None or approval["status"] != "consumed":
        return None
    if persisted.get("binding") != binding or persisted.get("tool") != tool:
        raise ToolError("approval consumption is bound to a different request")
    if _consumption_completed(conn, row["ulid"]):
        ports = _task_binding_ports.get()
        if ports is None or ports[2](conn) is None:
            raise ToolError("committed approval requires its verified task receipt replay")
        # A competing initial call committed while this call was looking for
        # authority. Cache only that verified read, never a new execution grant.
        receipts[_COMMITTED_TASK_REPLAY] = binding
        return {"gated": True, "replayed": True}
    latest = _latest_consumption(conn, approval["id"])
    if latest != row["ulid"] and latest is not None and _consumption_completed(conn, latest):
        # Another request with the same payload consumed this grant later and
        # finished with it. This binding's attempt is over: ask afresh rather
        # than wedge the key on a superseded consumption (#1980).
        return None
    if tool == "task_update":
        try:
            semantic_match = (
                _task_semantic_digest(json.loads(approval["payload"])) == binding["digest"]
                and _task_semantic_digest(json.loads(canon)) == binding["digest"]
            )
        except (TypeError, ValueError) as exc:
            raise ToolError("approval has no valid semantic request binding") from exc
        if not semantic_match:
            raise ToolError("approval consumption is bound to different semantic arguments")
    elif approval["payload"] != canon:
        raise ToolError("native approval payload changed before recovery")
    owned = {
        "gated": True,
        "approval_id": approval["id"],
        "tool": tool,
        "payload": approval["payload"],
        "generation": persisted.get("generation"),
        "restored": False,
        "_consumption_nonce": row["ulid"],
        "_grant": persisted.get("grant", {}),
        "_principal": {name: binding[name] for name in ("org_id", "actor", "subject")},
        "_recovery_binding": binding,
        # Adopted from the attempt that consumed it, which may still be
        # running: only that owner may restore the pass (#1980).
        "_recovered": True,
    }
    if set(owned["_grant"]) != set(_GRANT_FIELDS):
        raise ToolError("approval consumption has no valid durable grant binding")
    _validate_receipt(conn, (tool, approval["payload"]), owned)
    if owned["generation"] != approval["updated_at"]:
        raise ToolError("approval consumption has no valid durable grant binding")
    receipts[(approval["id"], owned["generation"])] = owned
    lease["cache"][receipt_key] = owned
    return {"gated": True, "approval_id": approval["id"]}


def _restore_receipt(receipt):
    if receipt.get("_recovered"):
        return
    restore_consumed_approval(
        int(receipt["approval_id"]),
        consumption_nonce=receipt.get("_consumption_nonce"),
        expected_grant=receipt.get("_grant"),
    )


def restore_consumed_approval(
    approval_id: int, *, consumption_nonce: str | None = None, expected_grant: dict | None = None
) -> None:
    """Restore only the consumption owned by this invocation, revoking its cache.

    Each consumption has a distinct updated_at generation, also used as the
    compare-and-set guard. Rollback of an old scope cannot restore a later
    isolated consumption of the same human pass.
    """
    receipts = _invocation_passes.get()
    owned = None
    if receipts is not None:
        lease = _invocation_lease.get()
        if (lease is None or not lease["active"]) and consumption_nonce is None:
            return
        receipts[_JOIN_AVAILABLE] = False
        receipts.get(_BOUND_CACHE, {}).clear()
        for receipt in receipts.values():
            if (
                isinstance(receipt, dict)
                and receipt.get("approval_id") == approval_id
                and not receipt.get("restored")
            ):
                owned = receipt
                break
        if owned is None and consumption_nonce is None:
            return
        if owned is not None and owned.get("_recovered"):
            # Adopted from the attempt that consumed it: only that owner may
            # restore the pass, whichever restore path is asking (#1980).
            return
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if (
            consumption_nonce is not None
            and _latest_consumption(conn, approval_id) != consumption_nonce
        ):
            return
        tenant_row = conn.execute(
            "SELECT 1 FROM approvals WHERE id=? AND org_id IS ?", (approval_id, _tenant())
        ).fetchone()
        if tenant_row is None:
            return
        owned_nonce = consumption_nonce or (owned or {}).get("_consumption_nonce")
        if _consumption_completed(conn, owned_nonce or _latest_consumption(conn, approval_id)):
            return
        if expected_grant is not None:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or any(row[field] != expected_grant[field] for field in _GRANT_FIELDS):
                return
        if owned is not None:
            conn.execute(
                "UPDATE approvals SET status='approved' "
                "WHERE id=? AND status='consumed' AND updated_at=?",
                (approval_id, owned["generation"]),
            )
            # Keep the generation on the row, so the next consumption is newer
            # even when the clock is frozen or moves backwards.
            owned["restored"] = True
        else:
            conn.execute(
                "UPDATE approvals SET status='approved' WHERE id=? AND status='consumed'",
                (approval_id,),
            )
        conn.commit()
    finally:
        conn.close()


# Tenant fence for every approval read and resolve (task #1793). Same rule as
# tasks and memory: `IS` so the untenanted private hub (tenant None) keeps
# matching NULL rows. Another tenant's row reads exactly like a missing one,
# and its pass or denial never applies to this tenant's call.
_TENANT_SQL = "org_id IS ?"


def _tenant() -> str | None:
    return edition.tenant_org_id()


def _caller_run_id(conn) -> str | None:
    """The verified caller's current run, or None when it is not unique.

    Display linkage only. ``approval_resolve`` and the gate match on tool,
    payload, status and tenant — never on this value.

    The caller is ``config.verified_agent_identity()``. A caller-supplied
    ``requested_by`` is ignored. With no verified identity the result is None.

    Priority, all inside this tenant:

    1. Exactly one ``tasks.current_run_id`` on a ``doing`` task whose
       ``current_run_agent`` is that identity. The row is either claimed by
       the same identity on a live lease (``lock_expires_at`` null or later
       than now) or unclaimed and still inside the implicit doing lease
       (``run_records.UNCLAIMED_LIVE_DOING_SQL``). That cache is the open
       self-pickup run (``sp-``…).
    2. Otherwise, exactly one live ``dispatch_runs`` row for that identity.
    3. Otherwise None. Two or more candidates in the source that would have
       decided, or a value that is not a bounded run id, do not fall through
       to a guess.
    """
    agent = config.verified_agent_identity()
    if not isinstance(agent, str) or not agent.strip():
        return None
    tenant = _tenant()
    # Lazy: run_records sits above this gate. The lookup needs only the shared
    # unclaimed-doing predicate, and run_records does not import this module.
    from .. import run_records

    now = storage.now()
    claimed = conn.execute(
        "SELECT DISTINCT current_run_id FROM tasks "
        "WHERE status='doing' AND current_run_agent=? "
        "AND current_run_id IS NOT NULL AND org_id IS ? AND ("
        "(claimed_by=? AND (lock_expires_at IS NULL OR lock_expires_at > ?)) OR ("
        f"{run_records.UNCLAIMED_LIVE_DOING_SQL}))",
        (agent, tenant, agent, now, run_records.unclaimed_doing_cutoff(now)),
    ).fetchall()
    claimed_ids = {events.bounded_run_id(row["current_run_id"]) for row in claimed}
    claimed_ids.discard(None)
    if len(claimed_ids) > 1 or (claimed and len(claimed_ids) != len(claimed)):
        # More than one run, or a cached value this store will not treat as a
        # run id. Either way the caller's run is not determined.
        return None
    if len(claimed_ids) == 1:
        return next(iter(claimed_ids))

    dispatched = conn.execute(
        "SELECT DISTINCT run_id FROM dispatch_runs "
        "WHERE agent_label=? "
        f"AND ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) "
        "AND task_id IN (SELECT id FROM tasks WHERE org_id IS ?)",
        (agent, tenant),
    ).fetchall()
    dispatch_ids = {events.bounded_run_id(row["run_id"]) for row in dispatched}
    dispatch_ids.discard(None)
    if len(dispatch_ids) == 1 and len(dispatch_ids) == len(dispatched):
        return next(iter(dispatch_ids))
    return None


def _emit_approval_requested(
    approval_id: int, tool: str, requested_by: str | None, run_id: str | None
) -> None:
    """Best-effort request fan-out. The row is already committed."""
    try:
        notification_tool = events.approval_notification_tool(tool)
        storage.record_agent_event(
            "approval",
            ref_id=str(approval_id),
            agent=config.verified_agent_identity(),
            summary=f"queued: {notification_tool}" if notification_tool else "queued",
            approval_tool=tool,
            run_id=run_id,
        )
    except Exception:
        pass
    approval_push.notify(approval_id)  # off unless configured; never raises
    from .. import approval_webhook

    approval_webhook.notify_requested(int(approval_id), tool, requested_by)
    try:
        # Task #2303: Slack card, off unless UA_APPROVAL_SLACK_CHANNEL is set.
        # Queued to its own worker; the row above has already committed.
        from .. import approval_slack

        approval_slack.notify_requested(int(approval_id), tool, requested_by)
    except Exception:
        logger.warning("approval slack card unavailable for #%s", approval_id, exc_info=True)


def get_approval(approval_id: int) -> dict | None:
    """Return the approval row for ``approval_id`` (payload decoded), or None.

    Read-only accessor for surfaces that resolve a queued row without having
    created it — the elicitation adapter (task #829) needs the tool, payload and
    requester to render the in-client form.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            f"SELECT * FROM approvals WHERE id=? AND {_TENANT_SQL}", (approval_id, _tenant())
        ).fetchone()
        return _row(row) if row is not None else None
    finally:
        conn.close()


#: Ids per ``IN (...)`` query in :func:`get_approvals`. Well under SQLite's
#: bound-parameter limit on every supported build.
_GET_APPROVALS_CHUNK = 500


def get_approvals(approval_ids) -> dict[int, dict]:
    """Return ``{id: row}`` for every ``approval_ids`` row that exists (task #1786).

    The batch form of :func:`get_approval`: same columns, same decoded
    ``payload``, one connection for the whole set. A list view that called
    ``get_approval`` once per row opened one connection per row, and each
    ``storage.connect()`` also loads the vector extension, so a 56-row queue
    cost 59 connects per poll. Missing ids are simply absent from the result.
    """
    ids = sorted({int(approval_id) for approval_id in approval_ids})
    if not ids:
        return {}
    out: dict[int, dict] = {}
    conn = storage.connect()
    try:
        for start in range(0, len(ids), _GET_APPROVALS_CHUNK):
            chunk = ids[start : start + _GET_APPROVALS_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM approvals WHERE id IN ({placeholders}) AND {_TENANT_SQL}",
                [*chunk, _tenant()],
            ).fetchall():
                decoded = _row(row)
                out[int(decoded["id"])] = decoded
        return out
    finally:
        conn.close()


def lookup_denied_approval(tool: str, payload: dict | None) -> tuple[int, str] | None:
    """Return the canonical denied approval row for an exact tool+payload match."""
    canon = _canonical_payload(payload)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, reason FROM approvals WHERE tool=? AND payload=? AND status='denied' "
            f"AND {_TENANT_SQL} ORDER BY id DESC LIMIT 1",
            (tool, canon, _tenant()),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"]), row["reason"] or "no reason given"
    finally:
        conn.close()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


#: Longest payload rendering kept for an inert (expired) pass.
_ELIDED_PAYLOAD_MAX = 240


def _elide_payload(payload: dict | None) -> dict:
    """Summarise an expired pass's payload instead of reproducing it (task #596).

    Delegation payloads carry a full multi-KB ``task_spec``. Fourteen of them
    dominated a single ``approval_pending()`` response while authorizing
    nothing. An expired pass only needs to be identifiable — which tool, which
    target, roughly what — not re-readable in full; the complete payload is
    still on the row in the database and in the audit trail.

    Short payloads pass through untouched, so nothing is hidden that was not
    genuinely bulky.
    """
    if not isinstance(payload, dict) or not payload:
        return payload if isinstance(payload, dict) else {}
    out: dict = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > _ELIDED_PAYLOAD_MAX:
            out[key] = f"{value[:_ELIDED_PAYLOAD_MAX]}… [{len(value)} chars, elided]"
        else:
            out[key] = value
    return out


def _pass_expires_at(row) -> datetime | None:
    """When an approved pass goes stale, or None if it cannot be dated.

    Measured from resolved_at (when the human said yes), falling back to
    created_at for rows predating that column being populated. An undateable row
    is treated as expired by the caller — failing closed, since the whole point
    is that a pass of unknown age must not authorize anything.
    """
    granted = _parse_ts(row["resolved_at"]) or _parse_ts(row["created_at"])
    if granted is None:
        return None
    return granted + timedelta(hours=config.approval_pass_ttl_hours())


def _request_dispatch_approval(payload: dict, requested_by: str | None) -> dict:
    """Queue against the same latest tenant-bound authority used by dispatch.

    Only the request path uses this selector; human resolution, execution
    consumption and recovery continue through their existing store writers.
    """
    from ..dispatch_reads import (
        DISPATCH_APPROVAL_TOOL,
        dispatch_approval_selection,
        dispatch_plan_blocker,
    )

    if (
        not isinstance(payload, dict)
        or set(payload) != {"action", "task_id", "delegation_id"}
        or payload["action"] != "dispatch"
        or any(
            type(payload[key]) is not int or payload[key] < 1
            for key in ("task_id", "delegation_id")
        )
        or not requested_by
        or requested_by != config.verified_agent_identity()
    ):
        raise ToolError("execution approval requires the exact pair and verified requester")
    conn = storage.connect()
    created = False
    run_id = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        valid, grant = dispatch_approval_selection(
            conn, task_id=payload["task_id"], delegation_id=payload["delegation_id"]
        )
        if not valid:
            raise ToolError("execution approval authority is invalid or ambiguous")
        pair = conn.execute(
            "SELECT t.notes, d.task_spec FROM tasks t JOIN delegations d ON d.task_id=t.id "
            "WHERE t.id=? AND d.id=?",
            (payload["task_id"], payload["delegation_id"]),
        ).fetchone()
        blocker = dispatch_plan_blocker(pair["notes"], pair["task_spec"])
        if blocker:
            raise ToolError(f"execution approval requires operator revision of the {blocker} plan")
        if grant is not None:
            if grant["status"] in {"pending", "denied"}:
                return {
                    "gated": True,
                    "approval_id": int(grant["id"]),
                    "status": grant["status"],
                    "reason": grant["reason"],
                }
            if grant["status"] == "approved":
                expires = _pass_expires_at(grant)
                if expires is not None and datetime.now(UTC) < expires:
                    return {"gated": True, "approval_id": int(grant["id"]), "status": "approved"}
                conn.execute(
                    "UPDATE approvals SET status='expired', updated_at=?, reason=? WHERE id=?",
                    (
                        storage.now(),
                        "one-shot pass expired unconsumed; re-request approval",
                        grant["id"],
                    ),
                )
            elif grant["status"] not in {"consumed", "expired", "revoked"}:
                raise ToolError("execution approval has an unsupported status")
        # Never resurrect an older pass or pending request hidden by a later
        # consumed/revoked/expired row. Identical concurrent requests share this lock.
        ts = storage.now()
        run_id = _caller_run_id(conn)
        cur = conn.execute(
            "INSERT INTO approvals (tool,payload,requested_by,status,created_at,updated_at,"
            "org_id,run_id) VALUES (?,?,?,'pending',?,?,?,?)",
            (
                DISPATCH_APPROVAL_TOOL,
                _canonical_payload(payload),
                requested_by,
                ts,
                ts,
                edition.tenant_org_id(),
                run_id,
            ),
        )
        approval_id = cur.lastrowid
        created = True
        conn.commit()
    finally:
        conn.close()
    if created:
        _emit_approval_requested(int(approval_id), DISPATCH_APPROVAL_TOOL, requested_by, run_id)
    return {"gated": True, "approval_id": int(approval_id), "status": "pending"}


_EXPLICIT_NATIVE_EXECUTION_TOOLS = frozenset({"fs_write"})


#: Operator self-approval (task #2500; explicit operator approval, 2026-11-12).
#: On a one-human hub an approval needs a *different* human, so a write the
#: verified operator makes from their own browser parks with nobody able to
#: approve it. For exactly these actions the operator's own verified submission
#: is recorded as a pass instead of an unanswerable request. Named list only:
#: separation of duties stays for work-product release, config revision
#: commit/rollback, harness config apply and org proposal apply, which are not
#: here. `langgraph_governed_call` is the approval tool behind the LangGraph
#: workflow run; `task_delete` is the forced production action.
OPERATOR_SELF_APPROVAL_TOOLS = frozenset({"task_delete", "langgraph_governed_call"})

#: Why a self-approved row exists, written into the row and the ledger event.
SELF_APPROVAL_REASON = (
    "operator self-approval (task #2500): the verified human operator's own "
    "write, on a hub with one human, so no different person could approve it"
)

#: Hash-chained record of every operator self-approval, beside the consumption
#: event. One row per pass, naming the tool, the operator and the reason.
CHANNEL_OPERATOR_SELF_APPROVAL = "agent.approval.self_approval"


def _operator_self_approval_applies(tool: str, requested_by: str | None) -> bool:
    """Whether this call is the verified human operator's own, for a named tool.

    Keyed off the transport-verified identity (`config.verified_caller_is_operator`,
    the same definition delegate()'s human skip uses), never a caller-supplied
    label, so an agent holding its own bearer token can never reach it by naming
    the operator. ``requested_by``, when present, must be the same person: a
    call that names somebody else is not the operator's own write and stays on
    the ordinary pending-approval path.
    """
    if tool not in OPERATOR_SELF_APPROVAL_TOOLS:
        return False
    if not config.verified_caller_is_operator():
        return False
    return not requested_by or _same_person(config.verified_agent_identity(), requested_by)


def _effective_requester(requested_by: str | None) -> str | None:
    """Who this call acts as: the transport-verified identity, else the claimed label.

    The registered wrapper always passes ``config.verified_agent_identity()``;
    an in-process caller may pass a label instead. A claimed label never
    outranks a verified identity, the same rule ``_receipt_key`` applies.
    """
    return config.verified_agent_identity() or requested_by


def _pass_belongs_to(row, requester: str | None) -> bool:
    """Whether ``row``'s one-shot authority belongs to this caller (task #2492).

    A row is matched on tool, payload and tenant, which does not identify a
    person: without this check a second eligible caller making the identical
    call spent the first caller's pass. Two parties may spend it, and nobody
    else:

    * the **requester** that queued it (aliases of one human count as the same
      person, ``_same_person``);
    * the **human who resolved it** — the hub's human-in-the-loop flows are
      exactly this shape: an agent asks, a person accepts the form or resolves
      the request, and that person's own session then spends the pass
      (elicitation, the GitHub App check, the Control Center writes).

    A row that named nobody stays unbound: it carries no requester to bind,
    which is the in-process path where the caller's tool supplied no label —
    the registered wrapper always names the verified identity, so a pass
    created through it is always bound.
    """
    owner = row["requested_by"]
    if owner is None:
        return True
    if requester is None:
        return False
    if _same_person(owner, requester):
        return True
    return _same_person(row["resolved_by"], requester)


def _consume_approved(
    conn,
    tool: str,
    canon: str,
    approved,
    requested_by: str | None,
    receipts: dict | None,
    lease: dict | None,
    cache: dict,
    receipt_key: tuple,
) -> dict | None:
    """Consume one approved row as this call's one-shot pass (task #62 · C).

    The compare-and-swap on ``updated_at`` means two concurrent callers cannot
    spend the same pass: the loser gets ``None`` and continues to the next row.
    Extracted so the operator's own pass (task #2500) is consumed by the same
    code every other pass goes through.
    """
    previous = _parse_ts(approved["updated_at"])
    generation = max(
        datetime.now(UTC),
        previous + timedelta(microseconds=1) if previous else datetime.min.replace(tzinfo=UTC),
    ).isoformat(timespec="microseconds")
    consumed = conn.execute(
        "UPDATE approvals SET status='consumed', updated_at=? "
        "WHERE id=? AND status='approved' AND updated_at=? AND requested_by IS ?",
        (generation, approved["id"], approved["updated_at"], approved["requested_by"]),
    )
    if consumed.rowcount != 1:
        conn.commit()
        return None
    consumption_payload = {"approval_id": int(approved["id"]), "tool": tool}
    binding = registered_task_binding() if tool in {"task_update", "task_untrusted_clear"} else None
    if binding is not None:
        consumption_payload.update(
            binding=binding,
            generation=generation,
            grant={field: approved[field] for field in _GRANT_FIELDS},
        )
    nonce, _, _ = events.record_event_once_on_connection(
        conn,
        _CONSUMPTION_CHANNEL,
        payload=consumption_payload,
        originator=config.verified_agent_identity() or requested_by,
    )
    conn.commit()
    receipt = {"gated": True, "approval_id": approved["id"]}
    if receipts is not None and lease is not None and lease["active"]:
        owned = {
            **receipt,
            "tool": tool,
            "payload": canon,
            "generation": generation,
            "restored": False,
            "_consumption_nonce": nonce,
            "_grant": {field: approved[field] for field in _GRANT_FIELDS},
            "_principal": _approval_principal(),
        }
        receipts[(approved["id"], generation)] = owned
        if receipts.get(_BOUND_ACTION, receipt_key) == receipt_key:
            cache[receipt_key] = owned
    return receipt


def _emit_operator_self_approval(approval_id: int, tool: str, operator: str) -> None:
    """Record the operator's own pass in the evidence ledger (task #2500).

    Best effort: the row and its consumption are already committed, so a failed
    write here must not fail the call — but it is logged rather than swallowed.
    """
    try:
        events.record_event(
            CHANNEL_OPERATOR_SELF_APPROVAL,
            payload={
                "approval_id": approval_id,
                "tool": tool,
                "operator": operator,
                "reason": SELF_APPROVAL_REASON,
            },
            originator=operator,
            dedupe_key=f"operator-self-approval:{approval_id}",
            notify=False,
        )
    except Exception:  # noqa: BLE001 - the pass already stands; never mask the call
        logger.warning("operator self-approval not recorded for #%s", approval_id, exc_info=True)


def check_approval_gate(
    tool: str,
    payload: dict | None,
    requested_by: str | None = None,
    force: bool = False,
    request_only: bool = False,
    *,
    on_behalf_of: str | None = None,
) -> dict:
    result = _check_approval_gate(
        tool, payload, requested_by, force, request_only, on_behalf_of=on_behalf_of
    )
    if not request_only and _native_execution_gate.get() == tool:
        if tool not in _EXPLICIT_NATIVE_EXECUTION_TOOLS:
            mark_governed_execution()
        _native_execution_gate.set(None)
    return result


def _check_approval_gate(
    tool: str,
    payload: dict | None,
    requested_by: str | None,
    force: bool,
    request_only: bool,
    *,
    on_behalf_of: str | None = None,
) -> dict:
    """Chokepoint called by gated tools before executing (task #62 · C).

    No-op unless `tool` is listed in UA_APPROVAL_TOOLS. When gated:
    - an 'approved' row matching this exact (tool, payload) and this requester
      is consumed (one-shot pass) and the call proceeds (task #2492);
    - a 'denied' row matching it raises with the denial reason;
    - otherwise a 'pending' row is created (or this caller's identical pending
      request is reused — no row spam on retries) and ApprovalPendingError is
      raised.

    Returns {gated, approval_id?}. Never bypasses the tool's own safety
    checks — callers must run those first.
    """
    if not force and tool not in config.approval_gated_tools():
        return {"gated": False}

    if request_only and tool == "dispatch_claim":
        return _request_dispatch_approval(payload, requested_by)

    replay_binding = _task_replay_binding.get() or (_invocation_passes.get() or {}).get(
        _COMMITTED_TASK_REPLAY
    )
    if tool == "task_update" and replay_binding is not None:
        if replay_binding != registered_task_binding():
            raise ToolError("committed task replay binding changed")
        # Policy denies/rate limits still run; only this exact committed write's
        # one-shot authority is unnecessary for reading its original outcome.
        return {"gated": True, "replayed": True}
    canon = _canonical_payload(payload)
    receipt_key = _receipt_key(tool, payload, requested_by)
    # The identity this call acts as, for the pass that is its own (#2492).
    # ``on_behalf_of`` is the one explicit exception: a server-side caller that
    # spends a pass recorded for the identity it names (the org-proposal apply,
    # whose pass names the proposer and whose applier must be a different human).
    caller = on_behalf_of or _effective_requester(requested_by)
    receipts = _invocation_passes.get()
    lease = _invocation_lease.get()
    # Bound scopes cache only their action; legacy unbound scopes retain internal
    # same-gate rechecks. Unique records account for every independent pass.
    cache = lease["cache"] if lease is not None and lease["active"] else {}
    cached = cache.get(receipt_key)
    if cached is not None and not cached.get("restored"):
        if "_consumption_nonce" in cached:
            conn = storage.connect()
            try:
                conn.execute("BEGIN")
                validation_key = receipt_key
                if "_recovery_binding" in cached:
                    binding = registered_task_binding()
                    if binding != cached["_recovery_binding"]:
                        raise ToolError("recovered approval request binding changed")
                    if (
                        tool == "task_update"
                        and _task_semantic_digest(payload) != binding["digest"]
                    ):
                        raise ToolError("recovered approval semantic arguments changed")
                    validation_key = (tool, cached["payload"])
                _validate_receipt(
                    conn, validation_key, cached, require_live=not _completed_consumption_read.get()
                )
            finally:
                conn.close()
        return {"gated": True, "approval_id": cached["approval_id"]}
    ts = storage.now()
    conn = storage.connect()
    try:
        if not request_only:
            recovered = _recover_bound_consumption(conn, tool, canon, receipt_key, receipts, lease)
            if recovered is not None:
                return recovered
        now = datetime.now(UTC)
        # Operator self-approval (task #2500, explicitly approved). The verified
        # human operator's own write for one of OPERATOR_SELF_APPROVAL_TOOLS is
        # recorded and spent here, instead of parking as a request no different
        # person could ever approve. The row and its ledger event keep the audit
        # trail the ordinary path has, and the pass is consumed through the same
        # compare-and-swap, so it is still one-shot.
        if not request_only and _operator_self_approval_applies(tool, requested_by):
            operator = config.verified_agent_identity() or requested_by or ""
            requester = requested_by or operator
            minted = conn.execute(
                "INSERT INTO approvals (tool, payload, requested_by, status, reason, "
                "resolved_by, created_at, updated_at, resolved_at, org_id, run_id) "
                "VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool,
                    canon,
                    requester,
                    SELF_APPROVAL_REASON,
                    operator,
                    ts,
                    ts,
                    ts,
                    edition.tenant_org_id(),
                    _caller_run_id(conn),
                ),
            )
            # Keep minting and consumption in one transaction. Publishing an
            # approved row here lets an identical agent call spend the operator's
            # pass before this call's CAS. Only the consumed row may be visible.
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (minted.lastrowid,)).fetchone()
            receipt = _consume_approved(
                conn, tool, canon, row, requester, receipts, lease, cache, receipt_key
            )
            if receipt is None:
                raise ToolError("operator self-approval could not be consumed")
            _emit_operator_self_approval(int(row["id"]), tool, operator)
            return receipt
        approved_rows = conn.execute(
            "SELECT * FROM approvals WHERE tool=? AND payload=? AND status='approved' "
            f"AND {_TENANT_SQL} ORDER BY id",
            (tool, canon, _tenant()),
        ).fetchall()
        for approved in approved_rows:
            if not _pass_belongs_to(approved, caller):
                # Another requester's pass (#2492): leave it for them. This
                # call neither spends it, nor expires it on their behalf.
                continue
            expires_at = _pass_expires_at(approved)
            if expires_at is None or now >= expires_at:
                # Stale pass: retire it rather than honouring it, and leave the
                # reason in the row — expiry has to be visible in the audit
                # trail, not a silent disappearance. Expired is deliberately not
                # 'denied': the human never refused this, so a later identical
                # request should go back to them rather than being auto-rejected
                # forever.
                when = expires_at.isoformat() if expires_at else "unknown grant time"
                conn.execute(
                    "UPDATE approvals SET status='expired', updated_at=?, reason=? "
                    "WHERE id=? AND status='approved'",
                    (
                        ts,
                        f"one-shot pass expired unconsumed (TTL "
                        f"{config.approval_pass_ttl_hours()}h, expired {when}); "
                        "re-request approval",
                        approved["id"],
                    ),
                )
                conn.commit()
                continue
            if request_only:
                return {
                    "gated": True,
                    "approval_id": int(approved["id"]),
                    "status": "approved",
                }
            receipt = _consume_approved(
                conn, tool, canon, approved, requested_by, receipts, lease, cache, receipt_key
            )
            if receipt is None:
                continue
            return receipt

        denied = conn.execute(
            "SELECT id, reason FROM approvals WHERE tool=? AND payload=? AND status='denied' "
            f"AND {_TENANT_SQL} ORDER BY id DESC LIMIT 1",
            (tool, canon, _tenant()),
        ).fetchone()
        if denied is not None:
            if request_only:
                return {
                    "gated": True,
                    "approval_id": int(denied["id"]),
                    "status": "denied",
                    "reason": denied["reason"] or "no reason given",
                }
            reason = denied["reason"] or "no reason given"
            raise ToolError(
                f"approval request {denied['id']} for {tool!r} was DENIED: {reason}. "
                "Do not retry this exact call; change the request or ask the operator."
            )

        # Re-check and insert under one write lock, so concurrent identical
        # requests converge on a single pending row instead of each inserting
        # its own (every row would otherwise become an independent pass).
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        if not request_only:
            # A competing identical call may have consumed (or committed)
            # since the first lookup/CAS. Recheck under the pending-row lock.
            recovered = _recover_task_consumption(conn, tool, canon, receipt_key, receipts, lease)
            if recovered is not None:
                conn.commit()
                return recovered
        pending_rows = conn.execute(
            "SELECT id, requested_by FROM approvals WHERE tool=? AND payload=? "
            f"AND status='pending' AND {_TENANT_SQL} ORDER BY id LIMIT 1",
            (tool, canon, _tenant()),
        ).fetchall()
        # A pending row is a request, not authority, so it is reused across
        # callers: the human answers one request for one exact call, and the
        # pass that results is still spendable only by the requester it names
        # (#2492). Two-phase flows (elicitation, the GitHub App check) depend on
        # this reuse.
        pending = next(iter(pending_rows), None)
        if pending is not None:
            approval_id = pending["id"]
            conn.commit()
        else:
            run_id = _caller_run_id(conn)
            cur = conn.execute(
                "INSERT INTO approvals (tool, payload, requested_by, status, "
                "created_at, updated_at, org_id, run_id) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (tool, canon, requested_by, ts, ts, edition.tenant_org_id(), run_id),
            )
            conn.commit()
            approval_id = cur.lastrowid
            _emit_approval_requested(int(approval_id), tool, requested_by, run_id)
        if request_only:
            return {"gated": True, "approval_id": int(approval_id), "status": "pending"}
        raise ApprovalPendingError(
            f"{tool!r} requires approval (global allowlist): request {approval_id} is queued. "
            f"A human resolves it with approval_resolve({approval_id}, 'approve'|'deny'); "
            "retry the identical call after approval to consume the one-shot pass.",
            approval_id=int(approval_id),
        )
    finally:
        conn.close()


def approval_pending(
    tool: str | None = None, limit: int = 100, newest_expired: bool = False
) -> dict:
    """List queued approval requests awaiting a human decision (task #62 · C).

    Returns pending rows oldest-first. Optional `tool` filter. The queue is
    only populated when UA_APPROVAL_TOOLS gates tools; see approval_resolve to
    act on a request.

    Unconsumed passes are split by whether they can still authorize anything
    (task #596):

    * ``approved_unconsumed`` — live passes. Each carries ``expires_at``,
      ``expires_in_hours``, and ``expired: False``. These are the ones worth
      revoking if they should not stand.
    * ``expired_passes`` — past their TTL, so inert. Still reported, but out of
      the actionable list and with bulky payload fields summarised rather than
      reproduced. Delegation payloads carry a full multi-KB ``task_spec``, and
      fourteen of these once dominated a single response while authorizing
      nothing.

    Both buckets are read-only views: listing the queue never retires a pass.
    ``check_approval_gate`` does that lazily when a matching call actually
    tries to use one, which is the right moment and keeps the revocation path
    from task #171 usable right up until then.

    ``newest_expired=True`` keeps the newest ``limit`` expired passes instead
    of the oldest (still listed oldest-first) and adds ``expired_passes_total``,
    the uncapped count. The Control Center shows the most recent inert passes
    and the full count; the default window is unchanged for every other caller.

    Returns {pending: [...], approved_unconsumed: [...], expired_passes: [...],
    gated_tools: [...], pass_ttl_hours}.
    """
    clause = f"WHERE {_TENANT_SQL} AND status=?"
    if tool is not None:
        clause += " AND tool=?"
    lim = max(1, min(limit, 500))
    conn = storage.connect()
    try:

        def fetch(status: str, *, order: str = "id", limit_rows: int | None = None) -> list[dict]:
            p: list = [_tenant(), status]
            if tool is not None:
                p.append(tool)
            sql = f"SELECT * FROM approvals {clause} ORDER BY {order}"
            if limit_rows is not None:
                p.append(limit_rows)
                sql += " LIMIT ?"
            return [_row(r) for r in conn.execute(sql, p).fetchall()]

        now = datetime.now(UTC)
        live: list[dict] = []
        expired: list[dict] = []
        # Classify BEFORE applying any bound. The split is Python-side, so a
        # LIMIT on this query would truncate first, and with enough old approved
        # rows the OLDEST fill the window and live passes never reach the caller
        # (task #1449: limit=25 returned no live pass while 43 inert ones sat in
        # the table). A finite *scan* bound fails the same way one level down —
        # 2,001 live grants with the oldest pushed past the bound is still a
        # consumable grant the queue cannot show (Devin review, PR #803). So read
        # the approved bucket whole, split it, and bound each list only after
        # that. The per-row cost here is a timestamp parse; the expensive part —
        # eliding bulky payloads — runs after the cap, on the rows that survive.
        for row in fetch("approved"):
            expires_at = _pass_expires_at(row)
            row["expires_at"] = expires_at.isoformat() if expires_at else None
            row["expires_in_hours"] = (
                round((expires_at - now).total_seconds() / 3600, 2) if expires_at else None
            )
            row["expired"] = expires_at is None or now >= expires_at
            if not row["expired"]:
                live.append(row)
                continue
            # Task #596: an expired pass authorizes nothing, so it does not
            # belong in the list an operator reads to decide what to act on.
            # With UA_APPROVAL_TOOLS empty nothing ever consumed these, so 14
            # accumulated — each carrying its full multi-KB task_spec, sitting
            # above the one request that needed a decision. A queue that reads
            # as noise is a queue people stop reading, and the gate's whole
            # value is that someone reads it.
            #
            # Deliberately NOT retired to status='expired' here. Reaping on a
            # read would make listing the queue mutate it, and would expire a
            # pass before a human could revoke it — the revocation path task
            # #171 added precisely so a stale grant can be answered explicitly.
            # check_approval_gate still retires these lazily when a matching
            # call arrives, which is the right moment: something is trying to
            # use the pass.
            expired.append(row)

        # Bounded only now that the split is done, oldest-first as before.
        live = live[:lim]
        expired_total = len(expired)
        expired = expired[-lim:] if newest_expired else expired[:lim]
        for row in expired:
            row["payload"] = _elide_payload(row.get("payload"))

        out = {
            "pending": fetch("pending", limit_rows=lim),
            # Live passes only — what a human might still need to revoke.
            "approved_unconsumed": live,
            # Inert grants, kept visible but out of the actionable set, and
            # with their payloads summarised rather than reproduced in full.
            "expired_passes": expired,
            "gated_tools": sorted(config.approval_gated_tools()),
            "pass_ttl_hours": config.approval_pass_ttl_hours(),
        }
        if newest_expired:
            out["expired_passes_total"] = expired_total
        return out
    finally:
        conn.close()


def _same_person(left: str | None, right: str | None) -> bool:
    """True when two labels are the same identity, including human aliases.

    The operator's login label and configured name are one person. An exact string compare
    would let that person approve their own request.
    """
    if not left or not right:
        return False
    if left == right:
        return True
    for canonical, aliases in config.identity_alias_allowlist().items():
        group = {canonical, *aliases}
        if left in group and right in group:
            return True
    return False


def may_approve(row, caller: str | None) -> tuple[str, str]:
    """Whether ``caller`` could approve ``row`` now: ``("yes"|"no"|"unknown", reason)``.

    The checks ``approval_resolve`` applies to an approve, read without writing
    (task #2514): the row is pending, the caller is a named human, and the caller
    is not the requester under ``_same_person`` (alias-aware). ``unknown`` when the
    caller has no verified identity or the row names no requester, so separation of
    duties cannot be shown. ``yes`` is a read; the approve call checks again.
    """
    if row["status"] != "pending":
        return "no", f"the request is {row['status']}, not pending"
    if not caller:
        return "unknown", "no verified identity on this request"
    if config.actor_kind_for_identity(caller) != "human":
        return "no", "approval resolution requires a human identity"
    if not row["requested_by"]:
        return "unknown", "the request names no requester, so separation of duties is unproven"
    if _same_person(caller, row["requested_by"]):
        return "no", "you asked for this; a different person must approve it"
    return "yes", "a different human may approve this request"


#: Outcome of the call a consumed pass authorized (task #2514), one event per
#: consumption, keyed by its nonce.
CHANNEL_CALL_OUTCOME = "agent.approval.call_outcome"


def _record_call_outcomes(receipts: dict, outcome: str) -> None:
    """Record ``completed`` or ``failed`` for every pass this invocation spent.

    Best effort: the call's own result or exception always wins over this write.
    A restored (unspent) receipt records nothing.
    """
    seen: set[str] = set()
    for receipt in tuple(receipts.values()):
        if not isinstance(receipt, dict) or receipt.get("restored"):
            continue
        nonce = receipt.get("_consumption_nonce")
        if not nonce or nonce in seen or receipt.get("approval_id") is None:
            continue
        seen.add(nonce)
        try:
            events.record_event(
                CHANNEL_CALL_OUTCOME,
                payload={
                    "approval_id": int(receipt["approval_id"]),
                    "nonce": nonce,
                    "tool": receipt.get("tool"),
                    "outcome": outcome,
                },
                originator=config.verified_agent_identity(),
                dedupe_key=f"approval-call-outcome:{nonce}",
                notify=False,
            )
        except Exception:  # noqa: BLE001 - the outcome is advisory; never masks the call
            logger.warning("approval call outcome not recorded for #%s", receipt["approval_id"])


def pass_view(row) -> dict:
    """``{state, expires_at, call_outcome}`` for an approval row (task #2514).

    ``state`` is the status, except an approved pass reads ``live`` inside its
    lifetime and ``expired`` past it (the gate retires it on its next look). ``expires_at`` is set for
    an approved pass. ``call_outcome`` is set for a consumed pass: ``completed``
    or ``failed`` as recorded when the authorized call finished, or ``unknown``
    when no governed invocation recorded one for the current consumption.
    """
    status = row["status"]
    out: dict = {"state": status, "expires_at": None, "call_outcome": None}
    if status == "approved":
        expires = _pass_expires_at(row)
        out["expires_at"] = expires.isoformat() if expires else None
        live = expires is not None and datetime.now(UTC) < expires
        out["state"] = "live" if live else "expired"
    elif status == "consumed":
        out["call_outcome"] = "unknown"
        conn = storage.connect()
        try:
            nonce = _latest_consumption(conn, row["id"])
            if nonce:
                found = conn.execute(
                    "SELECT json_extract(payload, '$.outcome') AS outcome FROM event_log "
                    "WHERE channel=? AND org_id IS ? AND json_extract(payload, '$.nonce')=? "
                    "ORDER BY ROWID DESC LIMIT 1",
                    (CHANNEL_CALL_OUTCOME, _tenant(), nonce),
                ).fetchone()
                if found is not None and found["outcome"] in ("completed", "failed"):
                    out["call_outcome"] = found["outcome"]
        finally:
            conn.close()
    return out


def _record_evidence_gap(
    kind: str,
    ref_id: int,
    task_id: object,
    exc: BaseException | None,
    *,
    tool: str | None = None,
    tier: str = "low",
    outcome: str = "proceeded",
) -> bool:
    """Make a missing producer stamp visible; return whether the ledger has it.

    ``exc`` is the exception the stamp raised, or None when it returned None
    (``stamp_authenticated_producer_event`` swallows its own failures, task
    #1672). ``outcome`` says what the resolution did: ``"proceeded"`` (it
    committed; only its signed attribution is missing) or ``"refused"`` (a
    high-tier approve that did not commit). Log at warning and append an
    ``audit.evidence.gap`` event naming the object and the exception type
    (never its message, which may carry payload or credential text). If even
    that append fails, the warning is all that is left, and it says so.
    """
    error_type = NO_PRODUCER_STAMP if exc is None else type(exc).__name__
    logger.warning(
        "evidence gap: producer stamp failed for %s %s (%s, %s tier, %s)",
        kind,
        ref_id,
        error_type,
        tier,
        outcome,
    )
    payload: dict[str, object] = {
        "kind": kind,
        "ref_id": str(ref_id),
        "task_id": None if task_id is None else str(task_id),
        "stage": "producer_stamp",
        "error_type": error_type,
        "tier": tier,
        "outcome": outcome,
    }
    if tool is not None:
        payload["tool"] = tool
    try:
        events.record_event(
            CHANNEL_EVIDENCE_GAP,
            payload=payload,
            originator=config.verified_agent_identity(),
        )
    except Exception:  # noqa: BLE001 - the warning above is the fallback record
        logger.warning("evidence gap for %s %s could not be recorded on the ledger", kind, ref_id)
        return False
    return True


def _approval_task_id(row) -> object:
    """The task an approval row is scoped to, for a task-scoped producer stamp."""
    try:
        payload_raw = row["payload"]
        payload_obj = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        if isinstance(payload_obj, dict):
            # Prefer explicit task_id; fall back to parent_task_id so gated
            # delegate payloads still stamp a task-scoped HMAC (task #1140
            # real-release wedge).
            if payload_obj.get("task_id") is not None:
                return payload_obj.get("task_id")
            if payload_obj.get("parent_task_id") is not None:
                return payload_obj.get("parent_task_id")
    except Exception:  # noqa: BLE001 - an unreadable payload stamps without a task
        return None
    return None


def _stamp_approval(
    approval_id: int, task_id: object, *, conn=None
) -> tuple[str | None, BaseException | None]:
    """Stamp the producer event; return ``(event_id, exc)``. A None event_id is a gap.

    ``conn`` appends the stamp inside the caller's open write transaction.
    """
    try:
        from ..audit.producer_sign import stamp_authenticated_producer_event

        return (
            stamp_authenticated_producer_event(
                kind="approval",
                ref_id=approval_id,
                task_id=task_id,
                **({"conn": conn} if conn is not None else {}),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - the caller decides refuse vs. record
        return None, exc


def _revocable_bound_consumption(conn, row) -> bool:
    if row["tool"] == "synthetic_workflow":
        nonce = _latest_consumption(conn, row["id"])
        return (
            nonce is not None
            and not _consumption_completed(conn, nonce)
            and conn.execute(
                "SELECT 1 FROM event_log WHERE channel='agent.synthetic_workflow' "
                "AND org_id IS ? AND json_extract(payload,'$.stage')='effect_intent' "
                "AND json_extract(payload,'$.approval._consumption_nonce')=? LIMIT 1",
                (_tenant(), nonce),
            ).fetchone()
            is not None
        )
    nonce = _latest_consumption(conn, row["id"])
    if nonce is None or _consumption_completed(conn, nonce):
        return False
    event = conn.execute(
        "SELECT payload,originator FROM event_log WHERE ulid=? AND channel=? AND org_id IS ?",
        (nonce, _CONSUMPTION_CHANNEL, _tenant()),
    ).fetchone()
    if event is None:
        return False
    producer = json.loads(event["payload"])
    binding = producer.get("binding")
    grant = producer.get("grant")
    return bool(
        row["tool"] in {"task_update", "task_untrusted_clear"}
        and producer.get("tool") == row["tool"]
        and isinstance(binding, dict)
        and binding.get("operation") == "task_update"
        and binding.get("version") == 1
        and binding.get("org_id") == _tenant()
        and binding.get("actor") == event["originator"]
        and isinstance(grant, dict)
        and set(grant) == set(_GRANT_FIELDS)
        and all(row[field] == grant[field] for field in _GRANT_FIELDS)
        and row["updated_at"] == producer.get("generation")
    )


def _record_receipt_intent(conn, approval_id: int, status: str, requester: str | None) -> None:
    """Write the requester's receipt intent inside the decision transaction (task #2394).

    A savepoint isolates it: if the intent write fails, only the intent rolls
    back and the decision commits without one (logged), never the reverse. The
    receipt is then late, not lost: the receipt sweep re-creates the intent
    from the committed decision (task #2484).
    """
    try:
        from .. import approval_receipts

        conn.execute("SAVEPOINT approval_receipt_intent")
        try:
            approval_receipts.record_intent(conn, approval_id, status, requester)
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT approval_receipt_intent")
            raise
        finally:
            conn.execute("RELEASE SAVEPOINT approval_receipt_intent")
    except Exception:
        logger.warning("approval receipt intent not recorded for #%s", approval_id, exc_info=True)


def _deliver_receipt(approval_id: int, status: str) -> None:
    """Hand the requester its receipt after the decision committed (task #2394)."""
    try:
        from .. import approval_receipts

        approval_receipts.deliver(approval_id, status)
    except Exception:
        logger.warning("approval receipt unavailable for #%s", approval_id, exc_info=True)


def approval_resolve(
    id: int,
    decision: str,
    resolved_by: str | None = None,
    reason: str | None = None,
) -> dict:
    """Approve, deny, or revoke an approval request (task #62 · C, #171).

    `decision` is 'approve', 'deny' or 'revoke'.

    - approve / deny act on a 'pending' row. Approving turns it into a one-shot
      pass: the next identical gated call consumes it and executes. Denying
      records `reason` and makes retries of that exact call fail with it.
    - revoke withdraws an approved pass, or verified human operators may revoke
      a producer-bound consumption still pending its task commit. The database
      write lock excludes committed/spent authority from this recovery case.
      For the original approved-but-unconsumed case, withdrawing the
      pass. Until #171 there was no way to do this: an approved row could only
      be resolved while 'pending', so a granted-but-unused pass sat in the queue
      as a standing one-shot authorization with no expiry and no revocation
      path — a permanent grant, contradicting the rule that approvals are not
      durable. A revoked pass is not consumable, and a later identical request
      re-queues for a fresh human decision rather than being auto-denied (the
      human withdrew a specific grant; they did not refuse the action forever).

    A verified human may also revoke a consumed dispatch grant while its
    exact consumption/run proof still authorizes recovery inside the original
    lifetime. Completed effects and unrelated consumed grants remain closed.

    `resolved_by` is the resolving label, recorded for the audit trail. An
    authenticated caller overwrites it. Approve requires that label to be a
    human and different from the requester; deny and revoke do not. Returns
    the updated approval row.
    """
    if decision not in ("approve", "deny", "revoke"):
        raise ToolError("decision must be 'approve', 'deny' or 'revoke'")
    # An authenticated caller is the authority for the audit row. In enforced
    # mode, a caller-supplied human name cannot stand in for authentication.
    verified = config.verified_agent_identity()
    if decision == "approve" and config.enforce_auth() and not verified:
        raise ToolError("approval resolution requires a verified human identity")
    resolved_by = verified or (resolved_by.strip() if resolved_by and resolved_by.strip() else None)
    ts = storage.now()
    conn = storage.connect()
    try:
        row = conn.execute(
            f"SELECT * FROM approvals WHERE id=? AND {_TENANT_SQL}", (id, _tenant())
        ).fetchone()
        if row is None:
            raise ToolError(f"no approval request with id {id}")
        if decision == "revoke":
            recovery = None
            pending_bound = False
            if row["status"] == "consumed":
                # Only consumed-grant recovery needs a lock before validation.
                # Ordinary approved-pass revoke retains its existing
                # consume-versus-revoke CAS.
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    f"SELECT * FROM approvals WHERE id=? AND {_TENANT_SQL}", (id, _tenant())
                ).fetchone()
                if current is None:
                    raise ToolError(f"no approval request with id {id}")
                from ..dispatch_reads import DISPATCH_APPROVAL_TOOL, dispatch_recovery_run

                if current["tool"] == DISPATCH_APPROVAL_TOOL:
                    fields = ("status", "updated_at", *_GRANT_FIELDS)
                    if any(current[field] != row[field] for field in fields):
                        raise ToolError(f"approval request {id} changed concurrently; re-read it")
                    expires = _pass_expires_at(current)
                    if (
                        verified
                        and config.actor_kind_for_identity(verified) == "human"
                        and expires is not None
                        and datetime.now(UTC) < expires
                    ):
                        recovery = dispatch_recovery_run(conn, current)
                row = current
                pending_bound = row["status"] == "consumed" and _revocable_bound_consumption(
                    conn, row
                )
            if row["status"] != "approved" and not pending_bound and recovery is None:
                raise ToolError(
                    f"approval request {id} is {row['status']!r}, not 'approved' or revocable pending authority; "
                    "only an approved-but-unconsumed pass or pending bound consumption can be revoked"
                )
            if pending_bound and (
                not verified or config.actor_kind_for_identity(verified) != "human"
            ):
                raise ToolError(
                    "revoking pending bound authority requires a verified human operator"
                )
        elif row["status"] != "pending":
            raise ToolError(
                f"approval request {id} is {row['status']!r}, not 'pending'; "
                "only pending requests can be approved or denied"
                + (" (use decision='revoke' to withdraw it)" if row["status"] == "approved" else "")
            )
        if decision == "approve" and resolved_by is None:
            raise ToolError(
                f"approval request {id} ({row['tool']}) needs a named approver; an "
                "anonymous approval cannot prove separation of duties"
            )
        if decision == "approve" and _same_person(resolved_by, row["requested_by"]):
            raise ToolError(
                f"approval request {id} ({row['tool']}) was requested by {row['requested_by']!r}; "
                "the approver must be a different person (separation of duties)"
            )
        if decision == "approve" and config.actor_kind_for_identity(resolved_by) != "human":
            raise ToolError(
                f"approval request {id} ({row['tool']}) cannot be approved by {resolved_by!r}; "
                "approval resolution requires a human identity"
            )
        status = {"approve": "approved", "deny": "denied", "revoke": "revoked"}[decision]
        # Evidence tier (task #1672). A verified resolver is expected to leave
        # a signed producer stamp. For a high-tier approve the stamp is part of
        # the resolution: it is appended inside the same transaction, after the
        # guarded UPDATE has won, so it commits with the status change or not at
        # all. A resolver that loses a concurrent race therefore never leaves a
        # stamp, and without a stamp the row stays pending.
        tier = evidence_tier(row["tool"])
        task_id = _approval_task_id(row)
        stamp_in_txn = bool(verified) and decision == "approve" and tier == "high"
        stamp_exc: BaseException | None = None
        refused = False
        try:
            updated = conn.execute(
                "UPDATE approvals SET status=?, resolved_by=?, reason=?, "
                f"updated_at=?, resolved_at=? WHERE id=? AND status=? AND updated_at=? AND {_TENANT_SQL}",
                (
                    status,
                    resolved_by,
                    reason,
                    ts,
                    ts,
                    id,
                    row["status"],
                    row["updated_at"],
                    _tenant(),
                ),
            )
            if updated.rowcount != 1:
                raise ToolError(
                    f"approval request {id} changed concurrently; retry after re-reading its state"
                )
            # Task #1909: the resolution goes on the hash-chained ledger in the
            # same transaction as the status change, so both commit or neither
            # does. The UPDATE above already holds the write lock, which
            # record_event_once_on_connection requires. The agent_events row
            # below stays for its readers but is no longer the only record.
            _, _, resolution_event = events.record_event_once_on_connection(
                conn,
                CHANNEL_APPROVAL_RESOLVED,
                payload={
                    "approval_id": id,
                    "tool": row["tool"],
                    "decision": decision,
                    "status": status,
                    "prior_status": row["status"],
                    "requested_by": row["requested_by"],
                    "resolved_by": resolved_by,
                    "resolver_verified": verified,
                    "payload_sha256": hashlib.sha256(
                        (row["payload"] or "").encode("utf-8")
                    ).hexdigest(),
                    "resolved_at": ts,
                },
                originator=verified,
                dedupe_key=f"approval-resolution:{id}:{status}",
            )
            # Task #2394: the requester's receipt intent commits with the
            # decision, so a rolled-back decision leaves no intent. It is
            # inside a savepoint: receipt plumbing must never block or undo a
            # decision, so a failure here is logged and the decision proceeds.
            _record_receipt_intent(conn, id, status, row["requested_by"])
            if stamp_in_txn:
                stamp_id, stamp_exc = _stamp_approval(id, task_id, conn=conn)
                refused = not stamp_id
            if refused:
                conn.rollback()
            else:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if refused:
            recorded = _record_evidence_gap(
                "approval",
                id,
                task_id,
                stamp_exc,
                tool=row["tool"],
                tier=tier,
                outcome="refused",
            )
            cause = NO_PRODUCER_STAMP if stamp_exc is None else type(stamp_exc).__name__
            ledger_note = (
                f"The gap is recorded as {CHANNEL_EVIDENCE_GAP}."
                if recorded
                else f"The gap could not be recorded as {CHANNEL_EVIDENCE_GAP} either; "
                "only the hub log warning has it."
            )
            raise ToolError(
                f"evidence gap: approval request {id} ({row['tool']}) is high-tier and "
                f"needs a signed producer stamp for {verified!r}, but stamping failed "
                f"({cause}); the request stays pending. Map a UA_AGENT_TOKENS bearer for "
                f"{verified!r} or sign in through the WorkOS leg, check the hub log for "
                f"the producer_sign error, then retry. {ledger_note}"
            ) from stamp_exc
        # Post-commit fan-out is best-effort; with the bus off (the pilot
        # deployment requires UA_EVENT_BUS=0) the call is skipped entirely.
        if resolution_event is not None and config.event_bus_enabled():
            events.publish(resolution_event)
        if not refused:
            from .. import approval_webhook

            approval_webhook.notify_decided(
                int(id),
                row["tool"],
                status,
                decision,
                row["requested_by"],
                resolved_by,
            )
            try:
                from .. import approval_slack

                approval_slack.notify_decided(int(id), status, resolved_by)
            except Exception:
                logger.warning("approval slack follow-up unavailable for #%s", id, exc_info=True)
            # Task #2394: tell the requesting agent. Best-effort after the
            # commit; queue hygiene's receipt sweep is the backstop.
            _deliver_receipt(int(id), status)
        if not refused and status in ("approved", "denied") and row["tool"] == LEDGER_TOOL:
            # Task #2234: publish this head's GitHub status now, not at the
            # next Approval Ledger poll. Queued to a background worker that
            # binds its own service identity; best-effort, the poll is the
            # backstop, and the resolution above has already committed.
            fast_path = ledger_fast_path()
            if fast_path is None:
                logger.info(
                    "approval ledger fast path not loaded in this process; "
                    "the scheduled poll publishes #%s",
                    id,
                )
            else:
                try:
                    fast_path(int(id), row["tool"], status)
                except Exception:
                    logger.warning(
                        "approval ledger fast path unavailable for #%s", id, exc_info=True
                    )
        try:
            storage.record_agent_event(
                "approval",
                ref_id=str(id),
                agent=resolved_by,
                summary=f"{status}: {row['tool']}",
            )
        except Exception:
            pass
        # Attribution wedge (#1155): stamp a signed producer event only when
        # the resolver is the verified hub identity — bearer-mapped agents use
        # hmac-sha256-v1; a resolver authenticated through the WorkOS hub leg
        # stamps the IdP-verified scheme instead (task #1249 / E2).
        # Every other verified resolution has committed; a stamp that raises or
        # returns None is recorded as a gap (and shown on /api/attention), never
        # a failure of the resolution itself.
        if verified and not stamp_in_txn:
            stamp_id, stamp_exc = _stamp_approval(id, task_id)
            if not stamp_id:
                _record_evidence_gap(
                    "approval",
                    id,
                    task_id,
                    stamp_exc,
                    tool=row["tool"],
                    tier=tier,
                    outcome="proceeded",
                )
        return _row(conn.execute("SELECT * FROM approvals WHERE id=?", (id,)).fetchone())
    finally:
        conn.close()
