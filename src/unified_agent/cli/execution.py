"""Controlled execution through the hub's existing gates (task #1938).

Every verb here maps to one existing hub tool or resource:

* ``delegate start`` -> ``delegation_manage`` ``create`` with an idempotency
  key that is persisted locally before the request is sent. The hub's
  approval gate, delegation spec standard and trust boundary still apply;
  the dispatch scheduler launches an executor only after its own
  ``dispatch_claim`` approval. The client never launches anything itself.
* ``delegate lookup`` / ``reconcile`` -> ``delegation_manage`` ``lookup``,
  which reads the caller's receipt with no effect.
* ``delegate cancel`` -> ``delegation_manage`` ``cancel`` with optional
  claim-generation and no-run fences.
* ``run cancel`` / ``run resume`` -> ``dispatch_cancel`` / ``dispatch_resume``.
* ``workflow list`` / ``workflow run`` -> ``workflow_list`` / ``workflow_run``.
* ``events follow`` -> ``event_log_query`` paged by ``after_seq``.

Waits are bounded and only read. Ending a wait, a timeout, Ctrl-C or a refused
credential never cancels work; the durable reference is printed instead.
Terminal states come from the platform object model declarations
(``object_model``), not from a list maintained here.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import receipts
from .commands import arguments, read_bytes
from .result import CliError, safe_text, strict_json

EXIT_IN_PROGRESS = 10
EXIT_UNKNOWN = 11
EXIT_NOT_COMMITTED = 12
EVENT_SCHEMA_VERSION = "ua.event/1"
# Consecutive lost polls a wait or follow absorbs before it stops.
MAX_POLL_FAILURES = 5
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Test seams: a wait or follow is driven by these, never by wall-clock sleeps.
_monotonic = time.monotonic
_sleep = time.sleep


def terminal_states(kind: str) -> frozenset[str]:
    from ..object_model import by_kind

    return frozenset(by_kind(kind).terminal_states)


def _context_refused(error: CliError, reference: dict, what: str) -> CliError:
    return CliError(
        error.code,
        f"{error.message} The {what} was not cancelled and continues on the hub.",
        error.exit_code,
        outcome=error.outcome,
        approval_id=error.approval_id,
        reference=reference,
    )


def wait_for(
    client,
    *,
    kind: str,
    read,
    field: str,
    state_field: str,
    budget: float,
    interval: float,
    reference: dict,
    resume_hint: str,
) -> dict:
    """Poll one object until a declared terminal state or the budget ends."""
    terminal = terminal_states(kind)
    deadline = _monotonic() + budget
    state = {"failures": 0, "last": None}
    try:
        return _poll(
            client,
            kind,
            read,
            field,
            state_field,
            terminal,
            deadline,
            interval,
            reference,
            resume_hint,
            state,
        )
    except KeyboardInterrupt:
        # Stopping the client is not an intervention: say so, and keep the handle.
        raise CliError(
            "interrupted",
            f"Stopped waiting; the {kind} was not cancelled. Resume with {resume_hint}.",
            EXIT_IN_PROGRESS,
            outcome="in_progress",
            reference={**reference, "status": state["last"]},
        ) from None


def _poll(
    client,
    kind,
    read,
    field,
    state_field,
    terminal,
    deadline,
    interval,
    reference,
    resume_hint,
    state,
):
    while True:
        try:
            snapshot = read()
            row = snapshot.get(field) if isinstance(snapshot, dict) else None
            if not isinstance(row, dict):
                raise CliError("invalid_response", f"Hub returned no {kind} object.", 1)
            state["failures"] = 0
            state["last"] = row.get(state_field)
            if state["last"] in terminal:
                return {"terminal": True, "status": state["last"], field: row}
        except CliError as error:
            if error.code not in {"transport", "timeout"}:
                raise _context_refused(error, reference, kind) from None
            state["failures"] += 1
            if state["failures"] > MAX_POLL_FAILURES:
                raise CliError(
                    "wait_interrupted",
                    f"Lost the hub connection while waiting; the {kind} was not cancelled. "
                    f"Resume with {resume_hint}.",
                    EXIT_IN_PROGRESS,
                    outcome="in_progress",
                    reference={**reference, "status": state["last"]},
                ) from None
            client.reconnect()
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise _budget_ended(kind, resume_hint, reference, state)
        _sleep(min(interval, remaining))
        # A sleep can land on the deadline: no read starts once the budget is spent.
        if _monotonic() >= deadline:
            raise _budget_ended(kind, resume_hint, reference, state)


def _budget_ended(kind, resume_hint, reference, state) -> CliError:
    return CliError(
        "in_progress",
        f"The wait budget ended before a terminal state; the {kind} continues and "
        f"was not cancelled. Resume with {resume_hint}.",
        EXIT_IN_PROGRESS,
        outcome="in_progress",
        reference={**reference, "status": state["last"]},
    )


def _delegation_uri(delegation_id: int) -> str:
    return f"unified-agent://delegations/{int(delegation_id)}"


def _read_delegation(client, delegation_id: int) -> dict:
    # delegation_manage get is the profile-visible read; the delegation
    # resource follows the hidden delegate_list alias for developer sessions.
    return client.tool("delegation_manage", {"action": "get", "params": {"id": int(delegation_id)}})


def _wait_delegation(client, args, delegation_id: int, reference: dict) -> dict:
    return wait_for(
        client,
        kind="delegation",
        read=lambda: _read_delegation(client, delegation_id),
        field="delegation",
        state_field="status",
        budget=args.wait_timeout,
        interval=args.poll_interval,
        reference=reference,
        resume_hint=f"ua delegate wait {int(delegation_id)}",
    )


def _committed_ids(data: Any) -> tuple[int, int]:
    if not isinstance(data, dict):
        raise CliError("invalid_response", "Hub returned no delegation result.", 1)
    delegation_id, task_id = data.get("delegation_id"), data.get("task_id")
    if type(delegation_id) is not int or type(task_id) is not int:
        raise CliError("invalid_response", "Hub returned an invalid delegation result.", 1)
    return delegation_id, task_id


def _record(receipt: dict) -> bool:
    """Save a receipt after its request was sent; False when it could not be saved.

    The request reached the hub (or may have), so a failed save must never
    become "nothing was sent": the caller still reports the hub's outcome.
    """
    try:
        receipts.save(receipt, phase=receipts.AFTER_SEND)
    except CliError as error:
        if error.code != "receipt_not_updated":
            raise
        return False
    return True


def _unsaved(receipt: dict, message: str) -> tuple[str, dict]:
    """The message and reference for an outcome whose receipt was not updated."""
    key = receipt["idempotency_key"]
    reference = {**receipts.public(receipt), "receipt_updated": False}
    return (
        f"{message} The local receipt could not be updated; keep the idempotency key {key}.",
        reference,
    )


def _commit(receipt: dict, data: Any) -> list[dict]:
    """Record a committed delegation; returns warnings for the caller's view."""
    delegation_id, task_id = _committed_ids(data)
    receipt.update(
        state="committed",
        delegation_id=delegation_id,
        task_id=task_id,
        uri=_delegation_uri(delegation_id),
        approval_id=None,
    )
    if not _record(receipt):
        # The hub committed: report the delegation and its key, exit 0.
        return [
            {
                "code": "receipt_not_updated",
                "message": receipts.not_updated(receipt["idempotency_key"]).message,
            }
        ]
    # Housekeeping only after a commit is recorded; it removes settled receipts.
    receipts.prune()
    return []


def _send_keyed(client, tool: dict, receipt: dict, args) -> dict:
    """Send the receipt's request with its key; record the outcome before returning."""
    key = receipt["idempotency_key"]
    payload = {
        "action": "create",
        "params": {**receipt["arguments"], "idempotency_key": key},
    }
    validated = arguments(tool, payload, {})
    try:
        data = client.tool(tool["name"], validated, write=True)
    except CliError as error:
        if error.code in {"timeout", "transport"}:
            receipt["state"] = "unknown"
            message = (
                "The delegation's outcome is unknown; it may have committed. Run "
                f"ua reconcile {key}. A retry with the same key never duplicates it."
            )
            reference = receipts.public(receipt)
            if not _record(receipt):
                message, reference = _unsaved(receipt, message)
            raise CliError(
                error.code,
                message,
                EXIT_UNKNOWN,
                outcome="unknown",
                reference=reference,
            ) from None
        if error.code == "approval_required":
            receipt.update(state="approval_required", approval_id=error.approval_id)
        elif error.code == "conflict":
            receipt["state"] = "conflict"
        else:
            receipt["state"] = "rejected"
        message, reference = error.message, receipts.public(receipt)
        if not _record(receipt):
            message, reference = _unsaved(receipt, message)
        raise CliError(
            error.code,
            message,
            error.exit_code,
            outcome=error.outcome,
            approval_id=error.approval_id,
            reference=reference,
        ) from None
    warnings = _commit(receipt, data)
    return _committed_view(client, receipt, data, args, warnings)


def _committed_view(client, receipt: dict, data: Any, args, warnings=()) -> dict:
    reference = receipts.public(receipt)
    view = {"outcome": "committed", "reference": reference, "result": data}
    if warnings:
        reference["receipt_updated"] = False
        view["warnings"] = list(warnings)
    if getattr(args, "wait", False):
        view["wait"] = _wait_delegation(client, args, receipt["delegation_id"], reference)
    return view


def _read_spec(source: str) -> str:
    try:
        text = read_bytes(source).decode("utf-8")
    except (OSError, UnicodeError):
        raise CliError(
            "validation", "The delegation spec must be a readable UTF-8 file.", 2
        ) from None
    if not text.strip():
        raise CliError("validation", "The delegation spec is empty.", 2)
    return text


def delegate_start(args, client, identity: dict, tool: dict) -> dict:
    params = {
        "task_spec": _read_spec(args.spec_file),
        "from_agent": identity["identity"],
        "to_agent": args.to,
        "priority": args.priority,
        "parent_task_id": args.parent,
        "deadline": args.deadline,
        "callback": args.callback,
        "project": args.project,
    }
    params = {key: value for key, value in params.items() if value is not None}
    if not args.to.strip():
        raise CliError("validation", "A delegation needs a non-empty --to label.", 2)
    # Validate the request shape before anything is persisted or sent.
    arguments(tool, {"action": "create", "params": params}, {})
    hub, label = client.hub_url, identity["identity"]
    digest = receipts.request_digest(hub, label, tool["name"], params)
    if args.idempotency_key is not None:
        key = receipts.validate_key(args.idempotency_key)
        try:
            receipt = receipts.load(key)
        except CliError as error:
            if error.code != "not_found":
                raise
            receipt = None
        if receipt is not None and receipt.get("request_digest") != digest:
            raise CliError(
                "validation",
                "This key was already used locally for a different request; use a new key.",
                2,
            )
    else:

        def make(new_key: str) -> dict:
            return receipts.prepare(
                hub=hub,
                identity=label,
                command="delegate.start",
                tool=tool["name"],
                arguments=params,
                key=new_key,
            )

        # Lookup and key minting are one critical section per fingerprint.
        receipt, reused = receipts.reserve(digest, make, force_new=args.new_key)
        key = receipt["idempotency_key"]
        if reused:
            print(f"ua: reusing the unsettled request key {key}", file=sys.stderr)
    if receipt is None:
        receipt = receipts.prepare(
            hub=hub,
            identity=label,
            command="delegate.start",
            tool=tool["name"],
            arguments=params,
            key=key,
        )
    # Persisted before sending: a crash after this point still knows the key.
    receipts.save(receipt)
    return _send_keyed(client, tool, receipt, args)


def _lookup(client, tool: dict, identity: dict, key: str) -> dict:
    payload = {
        "action": "lookup",
        "params": {"idempotency_key": key, "from_agent": identity["identity"]},
    }
    data = client.tool(tool["name"], arguments(tool, payload, {}), write=True)
    if not isinstance(data, dict) or data.get("state") not in {
        "committed",
        "approval_required",
        "in_progress",
        "not_found",
    }:
        raise CliError("invalid_response", "Hub returned an invalid receipt lookup.", 1)
    return data


def delegate_lookup(args, client, identity: dict, tool: dict) -> dict:
    return _lookup(client, tool, identity, receipts.validate_key(args.key))


def reconcile(args, client, identity: dict, tool: dict) -> dict:
    key = receipts.validate_key(args.key)
    receipt = receipts.load(key)
    if receipt.get("hub") != client.hub_url or receipt.get("identity") != identity["identity"]:
        raise CliError(
            "context_mismatch",
            "This receipt was sent to a different hub or as another identity; "
            "reconcile it from that context.",
            2,
            reference=receipts.public(receipt),
        )
    if receipt.get("tool") != tool["name"]:
        raise CliError("validation", "This receipt is not a delegation request.", 2)
    view = _lookup(client, tool, identity, key)
    state = view["state"]
    if state == "committed":
        warnings = _commit(receipt, view.get("result"))
        return _committed_view(client, receipt, view["result"], args, warnings)
    retry = args.retry and (state != "in_progress" or view.get("stale") is True)
    if retry:
        # The same key: the hub replays a commit it already holds, and runs
        # the request at most once otherwise.
        return _send_keyed(client, tool, receipt, args)
    # The lookup reached the hub, so a failed receipt update is reported with
    # the hub's answer and the key, never as "nothing was sent".
    if state == "approval_required":
        receipt.update(state="approval_required", approval_id=view.get("approval_id"))
        message = (
            "The delegation waits for human approval; after approval run "
            f"ua reconcile {key} --retry to consume the one-shot pass."
        )
        code, exit_code, approval_id = "approval_required", 9, view.get("approval_id")
    elif state == "in_progress":
        receipt["state"] = "in_progress"
        message = "The hub is still creating this delegation; reconcile again shortly."
        code, exit_code, approval_id = "in_progress", EXIT_IN_PROGRESS, None
    else:
        receipt["state"] = "not_committed"
        message = (
            f"The hub holds no record of this request; run ua reconcile {key} --retry to "
            "send it once with the same key."
        )
        code, exit_code, approval_id = "not_committed", EXIT_NOT_COMMITTED, None
    reference = receipts.public(receipt)
    if not _record(receipt):
        message, reference = _unsaved(receipt, message)
    raise CliError(
        code,
        message,
        exit_code,
        outcome=code,
        approval_id=approval_id,
        reference=reference,
    )


def delegate_show(args, client) -> dict:
    return _read_delegation(client, args.id)


def delegate_wait(args, client) -> dict:
    reference = {"delegation_id": args.id, "uri": _delegation_uri(args.id)}
    return _wait_delegation(client, args, args.id, reference)


def delegate_cancel(args, client, identity: dict, tool: dict) -> dict:
    params = {
        "id": args.id,
        "agent": identity["identity"],
        "reason": args.reason,
        "expected_claim_generation": args.expected_claim_generation,
        "require_no_run": True if args.require_no_run else None,
    }
    params = {key: value for key, value in params.items() if value is not None}
    payload = arguments(tool, {"action": "cancel", "params": params}, {})
    reference = {"delegation_id": args.id, "uri": _delegation_uri(args.id)}
    data = _intervene(client, tool, payload, reference)
    if not isinstance(data, dict) or data.get("cancelled") is not True:
        raise CliError(
            "conflict",
            "The delegation was not cancelled: it is no longer pending or in flight, a fence "
            "did not match, or you are not its delegator or assignee. Read it again.",
            8,
            outcome="conflict",
            reference=reference,
        )
    return {**data, "reference": reference}


def _intervene(client, tool: dict, payload: dict, reference: dict) -> Any:
    try:
        return client.tool(tool["name"], payload, write=True)
    except CliError as error:
        if error.code in {"timeout", "transport"}:
            raise CliError(
                error.code,
                "The intervention's outcome is unknown; read the object before retrying.",
                EXIT_UNKNOWN,
                outcome="unknown",
                reference=reference,
            ) from None
        raise CliError(
            error.code,
            error.message,
            error.exit_code,
            outcome=error.outcome,
            approval_id=error.approval_id,
            reference=reference,
        ) from None


def _run_uri(run_id: str) -> str:
    from urllib.parse import quote

    return f"unified-agent://runs/{quote(run_id, safe='')}"


def _run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value or ""):
        raise CliError("validation", "A run id is 1-128 letters, digits, '.', '_', ':' or '-'.", 2)
    return value


def run_intervene(args, client, tool: dict) -> dict:
    run_id = _run_id(args.id)
    data_in = {"run_id": run_id}
    if args.action == "cancel" and args.reason is not None:
        data_in["reason"] = args.reason
    reference = {"run_id": run_id, "uri": _run_uri(run_id)}
    data = _intervene(client, tool, arguments(tool, data_in, {}), reference)
    if isinstance(data, dict) and data.get("ok") is False:
        reason = data.get("reason")
        if isinstance(reason, str) and _REASON_CODE.fullmatch(reason):
            reference["reason"] = reason
        raise CliError(
            "conflict",
            "The hub did not change the run; read it again before retrying.",
            8,
            outcome="conflict",
            reference=reference,
        )
    return {"result": data, "reference": reference}


def run_wait(args, client) -> dict:
    run_id = _run_id(args.id)
    reference = {"run_id": run_id, "uri": _run_uri(run_id)}
    return wait_for(
        client,
        kind="run",
        read=lambda: client.resource(_run_uri(run_id)),
        field="run",
        state_field="state",
        budget=args.wait_timeout,
        interval=args.poll_interval,
        reference=reference,
        resume_hint=f"ua run wait {run_id}",
    )


_WORKFLOW_FAILURES = {
    "denied": ("permission_denied", "A workflow step was denied by policy.", 4, "failed"),
    "failed": ("workflow_failed", "A workflow step failed; inspect the step reports.", 1, "failed"),
    "indeterminate": (
        "unknown",
        "A workflow step's effect may have committed without its record; inspect it before "
        "running the workflow again.",
        EXIT_UNKNOWN,
        "unknown",
    ),
}


def workflow_run(args, client, tool: dict) -> dict:
    from .commands import load_input

    data_in: dict[str, Any] = {"name": args.name, "inputs": load_input(args.inputs)}
    if args.instance_id is not None:
        data_in["instance_id"] = args.instance_id
    reference: dict[str, Any] = {"workflow": args.name}
    if args.instance_id is not None:
        reference["instance_id"] = args.instance_id
    validated = arguments(tool, data_in, {})
    try:
        data = client.tool(tool["name"], validated, write=True)
    except CliError as error:
        if error.code in {"timeout", "transport"}:
            # workflow_run takes no idempotency key: never suggest a blind rerun.
            raise CliError(
                error.code,
                "The workflow's outcome is unknown and workflow_run has no retry key; "
                "check its events and objects before running it again.",
                EXIT_UNKNOWN,
                outcome="unknown",
                reference=reference,
            ) from None
        raise CliError(
            error.code,
            error.message,
            error.exit_code,
            outcome=error.outcome,
            approval_id=error.approval_id,
            reference=reference,
        ) from None
    if not isinstance(data, dict):
        raise CliError("invalid_response", "Hub returned no workflow result.", 1)
    if isinstance(data.get("instance_id"), str):
        reference["instance_id"] = data["instance_id"]
    failure = _WORKFLOW_FAILURES.get(data.get("status"))
    if failure:
        code, message, exit_code, outcome = failure
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        # Step names and states only: step errors are hub prose and stay unprinted.
        reference["steps"] = [
            {key: step.get(key) for key in ("name", "tool", "status", "attempts")}
            for step in steps
            if isinstance(step, dict)
        ]
        reference["status"] = data.get("status")
        raise CliError(code, message, exit_code, outcome=outcome, reference=reference)
    return data


# --- events follow ------------------------------------------------------------


def _read_cursor(path: Path) -> int | None:
    try:
        raw = path.read_bytes()[:256]
    except FileNotFoundError:
        return None
    except OSError:
        raise CliError("validation", "The cursor file is unreadable.", 2) from None
    try:
        value = strict_json(raw)
        seq = value.get("after_seq") if isinstance(value, dict) else value
    except (ValueError, UnicodeError, RecursionError):
        seq = None
    if type(seq) is not int or seq < 0:
        raise CliError("validation", "The cursor file holds no valid after_seq.", 2)
    return seq


def _write_cursor(path: Path, seq: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".cursor-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"schema_version": EVENT_SCHEMA_VERSION, "after_seq": seq}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError:
        raise CliError("validation", "Could not write the cursor file.", 2) from None


def _emit(record: dict, machine: bool) -> None:
    if machine:
        print(json.dumps(record, ensure_ascii=True, allow_nan=False), flush=True)
        return
    if record["type"] == "event":
        event = record["event"]
        print(
            "  ".join(
                safe_text(event.get(key, "—"))[:100]
                for key in ("seq", "channel", "originator", "created_at")
            ),
            flush=True,
        )
    else:
        print(f"{record['type']}: cursor {record.get('cursor')}", flush=True)


def follow(args, client, tool: dict, machine: bool) -> int:
    """Stream events as JSONL, resuming from the last emitted ``seq``.

    ``after_seq`` is the hub's gap-free commit order, so resuming from the last
    emitted seq after a lost connection yields no gap; a row at or below that
    seq is dropped, so a replayed page yields no duplicate.
    """
    cursor_file = Path(args.cursor_file).expanduser() if args.cursor_file else None
    cursor = args.after_seq
    if cursor is None and cursor_file is not None:
        cursor = _read_cursor(cursor_file)
    if cursor is None and args.from_start:
        cursor = 0
    if cursor is None:
        raise CliError(
            "validation",
            "Pick a start: --after-seq N, an existing --cursor-file, or --from-start.",
            2,
        )
    base: dict[str, Any] = {"limit": args.limit}
    if args.channel:
        base["channels"] = list(args.channel)
    if args.channel_prefix:
        base["channel_prefix"] = args.channel_prefix
    progress = {"cursor": cursor}
    try:
        return _follow(args, client, tool, machine, base, cursor_file, progress)
    except KeyboardInterrupt:
        if cursor_file is not None:
            _write_cursor(cursor_file, progress["cursor"])
        _emit(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "end",
                "reason": "interrupted",
                "cursor": progress["cursor"],
            },
            machine,
        )
        return 130


def _end(reason: str, cursor: int, machine: bool) -> int:
    _emit(
        {"schema_version": EVENT_SCHEMA_VERSION, "type": "end", "reason": reason, "cursor": cursor},
        machine,
    )
    return 0


def _follow(args, client, tool, machine, base, cursor_file, progress) -> int:
    cursor = progress["cursor"]
    deadline = _monotonic() + args.duration
    emitted = 0
    failures = 0
    while True:
        # No read starts once the duration is spent, after a sleep or a backoff.
        if _monotonic() >= deadline:
            if cursor_file is not None:
                _write_cursor(cursor_file, cursor)
            return _end("duration", cursor, machine)
        try:
            page = client.tool(tool["name"], arguments(tool, {**base, "after_seq": cursor}, {}))
            failures = 0
        except CliError as error:
            if error.code not in {"transport", "timeout"}:
                raise CliError(
                    error.code,
                    error.message,
                    error.exit_code,
                    outcome=error.outcome,
                    reference={"after_seq": cursor},
                ) from None
            failures += 1
            if failures > args.max_reconnects:
                raise CliError(
                    error.code,
                    f"Lost the hub connection; resume with --after-seq {cursor}.",
                    error.exit_code,
                    reference={"after_seq": cursor},
                ) from None
            if _monotonic() >= deadline:
                # A slow failed read can outlast the duration: end it as asked.
                if cursor_file is not None:
                    _write_cursor(cursor_file, cursor)
                return _end("duration", cursor, machine)
            print(f"ua: reconnecting from after_seq {cursor}", file=sys.stderr)
            client.reconnect()
            remaining = max(0.0, deadline - _monotonic())
            _sleep(min(args.poll_interval * failures, 30.0, remaining))
            continue
        rows = page.get("events") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise CliError("invalid_response", "Hub returned no event page.", 1)
        for row in rows:
            seq = row.get("seq") if isinstance(row, dict) else None
            if type(seq) is not int:
                raise CliError("invalid_response", "Hub returned an event without a seq.", 1)
            if seq <= cursor:
                continue
            _emit(
                {"schema_version": EVENT_SCHEMA_VERSION, "type": "event", "seq": seq, "event": row},
                machine,
            )
            cursor = progress["cursor"] = seq
            emitted += 1
            if args.max_events is not None and emitted >= args.max_events:
                break
        if cursor_file is not None:
            _write_cursor(cursor_file, cursor)
        reason = None
        if args.max_events is not None and emitted >= args.max_events:
            reason = "max_events"
        elif args.once:
            reason = "once"
        elif _monotonic() >= deadline:
            reason = "duration"
        if reason:
            return _end(reason, cursor, machine)
        # A full page means more is waiting: read it now instead of sleeping.
        if len(rows) < args.limit:
            _sleep(min(args.poll_interval, max(0.0, deadline - _monotonic())))
