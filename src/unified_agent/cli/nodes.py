"""``ua node``: device acceptance and node retirement for operators (task #2208).

These are ``coordination_manage`` actions over MCP, but the human operator's
MCP profile is read-only and cannot call them. The verbs therefore use the
Control Center's HTTP twins, with the same credential ``ua`` already resolved:

* ``ua node acceptance start|evaluate <node>`` -> ``POST /api/action/node-acceptance``
* ``ua node acceptance attest <node> --run-id --evidence-ref`` -> ``POST /api/action/node-acceptance``
* ``ua node acceptance status [<node>]``       -> ``GET /api/nodes/acceptance``
* ``ua node retire|unretire <node> --reason``  -> ``POST /api/action/node-retire``
* ``ua node list``                              -> ``GET /api/nodes`` (live and retired rows)

The hub decides who may act: the node's own agent or a lifecycle operator for
acceptance start/evaluate, an independent lifecycle operator for acceptance
attest, and lifecycle operators only for retire and unretire. Like every
``ua`` verb these never prompt; the required ``--reason`` or ``--evidence-ref``
is the deliberate step a write takes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .result import CliError, safe_text

ACCEPTANCE_PATH = "/api/action/node-acceptance"
ACCEPTANCE_STATUS_PATH = "/api/nodes/acceptance"
RETIRE_PATH = "/api/action/node-retire"
NODES_PATH = "/api/nodes"
# What ``ua node list`` keeps of each live row: identity, liveness and acceptance,
# not the launcher detail (harnesses, roots, recent prompt previews).
_LIST_FIELDS = ("node", "agent", "device_node", "version", "last_heartbeat", "acceptance")
# Retired rows are kept forever and only accumulate, so ``ua node list`` shows
# the first this many (the hub sorts them by name) and counts the rest under
# ``truncated`` (task #2210).
MAX_RETIRED_ROWS = 100
# Mirrors node_registry.MAX_RETIRE_REASON and node_acceptance.attest; the hub enforces both.
MAX_REASON = 500
MAX_NODE = 128
MAX_RUN_ID = 128
MAX_EVIDENCE_REF = 1000


def _node_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_NODE or any(ord(c) < 33 or ord(c) == 127 for c in name):
        raise CliError("validation", "A node name must be 1-128 visible characters.", 2)
    return name


def _run_id(value: Any) -> str:
    run_id = value.strip() if isinstance(value, str) else ""
    if not run_id or len(run_id) > MAX_RUN_ID or any(ord(c) < 33 or ord(c) == 127 for c in run_id):
        raise CliError("validation", "--run-id must be 1-128 visible characters.", 2)
    return run_id


def _evidence_ref(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise CliError("validation", "Attestation needs a non-empty --evidence-ref.", 2)
    if len(text) > MAX_EVIDENCE_REF:
        raise CliError(
            "validation", f"--evidence-ref must be at most {MAX_EVIDENCE_REF} characters.", 2
        )
    return text


def _reason(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise CliError("validation", "A retire or unretire needs a non-empty --reason.", 2)
    if len(text) > MAX_REASON:
        raise CliError("validation", f"--reason must be at most {MAX_REASON} characters.", 2)
    return text


def _detail(data: Any) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    return f" Hub said: {safe_text(error)[:300]}" if isinstance(error, str) and error else ""


def _refusal(status: int, data: Any, *, write: bool) -> CliError:
    """Map a hub refusal to the ``ua`` exit codes, with the hub's bounded reason."""
    detail = _detail(data)
    if status == 202:
        # routes._approval_pending parks a call as 202 {"approval_id": N,
        # "status": "pending", "code": "approval_required"}; older routes said
        # status "approval_pending". Either way it is not a failure.
        body = data if isinstance(data, dict) else {}
        approval_id = body.get("approval_id")
        approval_id = approval_id if type(approval_id) is int and approval_id > 0 else None
        if approval_id is not None or body.get("status") in {"pending", "approval_pending"}:
            where = f" #{approval_id}" if approval_id is not None else ""
            return CliError(
                "approval_required",
                f"The hub queued this call for human approval{where}; "
                "retry it unchanged once approved.",
                9,
                outcome="approval_required",
                approval_id=approval_id,
            )
    if status == 401:
        return CliError("authentication", "Hub rejected the credential; sign in again.", 3)
    if status == 403:
        return CliError(
            "permission_denied", "Hub denied this node action for your identity." + detail, 4
        )
    if status == 404:
        return CliError(
            "unavailable", "The hub knows no such node, or predates this command." + detail, 5
        )
    if status == 409:
        return CliError(
            "conflict",
            "The hub refused this node action as a conflict." + detail,
            8,
            outcome="conflict",
        )
    if status == 400:
        return CliError("validation", "Hub rejected the arguments." + detail, 2)
    verb = "write" if write else "read"
    return CliError("hub_rejected", f"Hub rejected the {verb} (HTTP {status})." + detail, 1)


def _call(client, method: str, path: str, body: dict | None = None, query: str = "") -> dict:
    write = method != "GET"
    try:
        status, data = client.platform(method, path, body, query=query)
    except CliError as error:
        if write and error.code in {"timeout", "transport"}:
            # The request may have committed: never suggest a blind retry.
            raise CliError(
                error.code,
                "The node action's outcome is unknown; check `ua node list` (retired rows "
                "and acceptance) before retrying.",
                error.exit_code,
            ) from None
        raise
    if 200 <= status < 300 and status != 202 and isinstance(data, dict):
        # Some reads report a failed store query in-band as 200 {..., "error": ...}
        # (GET /api/nodes/acceptance, GET /api/nodes). An empty list next to an
        # error is not an answer. No write body carries ``error`` on success.
        if isinstance(data.get("error"), str) and data["error"].strip():
            raise CliError(
                "hub_error", "The hub could not complete the request." + _detail(data), 1
            )
        return data
    if 200 <= status < 300 and status != 202:
        raise CliError("invalid_response", "Hub returned a non-object JSON payload.", 1)
    raise _refusal(status, data, write=write)


def execute(args, client) -> dict:
    """Run one ``ua node`` verb and return the hub's answer."""
    if args.action == "acceptance":
        verb = args.verb
        if verb == "status":
            node = getattr(args, "node", None)
            query = urlencode({"node": _node_name(node)}) if node is not None else ""
            return _call(client, "GET", ACCEPTANCE_STATUS_PATH, query=query)
        if verb == "attest":
            return _call(
                client,
                "POST",
                ACCEPTANCE_PATH,
                {
                    "action": "attest",
                    "node": _node_name(args.node),
                    "run_id": _run_id(args.run_id),
                    "evidence_ref": _evidence_ref(args.evidence_ref),
                },
            )
        return _call(
            client,
            "POST",
            ACCEPTANCE_PATH,
            {"action": verb, "node": _node_name(args.node)},
        )
    if args.action == "list":
        data = _call(client, "GET", NODES_PATH)
        live = data.get("nodes")
        retired = data.get("retired_nodes")
        retired_rows = [
            row for row in (retired if isinstance(retired, list) else []) if isinstance(row, dict)
        ]
        return {
            "nodes": [
                {key: row.get(key) for key in _LIST_FIELDS}
                for row in (live if isinstance(live, list) else [])
                if isinstance(row, dict)
            ],
            "retired_nodes": retired_rows[:MAX_RETIRED_ROWS],
            # Retired rows past the cap, left out above (0 when none were).
            "truncated": max(len(retired_rows) - MAX_RETIRED_ROWS, 0),
            "acceptance_required": data.get("acceptance_required") is True,
        }
    if args.action in {"retire", "unretire"}:
        return _call(
            client,
            "POST",
            RETIRE_PATH,
            {"action": args.action, "node": _node_name(args.node), "reason": _reason(args.reason)},
        )
    raise CliError("usage", "Unknown node action; use ua node --help.", 2)
