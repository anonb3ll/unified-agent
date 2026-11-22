"""Lightweight ua entry point for human operators and noninteractive agents."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from urllib.parse import quote

from .result import CliError, envelope, render, safe_text


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        # No prefix matching: an unknown flag such as --spec or --prompt must be
        # refused, never silently read as a longer flag like --spec-file.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # Avoid reflecting supplied values (including accidental secrets) in errors.
        raise CliError("usage", "Invalid arguments; use ua --help or the command's --help.", 2)


def _common(parser: argparse.ArgumentParser, root: bool = False) -> None:
    defaults = {} if root else {"default": argparse.SUPPRESS}
    parser.add_argument(
        "--hub",
        "--gateway",
        dest="hub",
        help="Explicit canonical HTTP(S) MCP gateway URL ending in /mcp",
        **defaults,
    )
    parser.add_argument(
        "--token-file",
        help="Existing private bearer file used by the gateway connection",
        **defaults,
    )
    parser.add_argument(
        "--identity", help="Credential label to select and verify; grants no authority", **defaults
    )
    parser.add_argument(
        "--project",
        help="Scope supported reads and new tasks to this project; guard claims and updates",
        **defaults,
    )
    parser.add_argument(
        "--session", help="Your domain mailbox session (not the MCP transport session)", **defaults
    )
    parser.add_argument(
        "--format", choices=("text", "json"), **({"default": "text"} if root else defaults)
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="HTTP read budget, seconds (1–120); default 10, or 60 for doctor, evidence, "
        "delegate start, reconcile and workflow run",
        **({"default": None} if root else defaults),
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt (all commands are noninteractive)",
        **defaults,
    )


def _validate_reply_intent(args) -> None:
    if args.family != "inbox" or getattr(args, "action", None) != "reply":
        return
    fields = [("reply target", args.message_id), ("recipient", args.to)]
    if args.reply_id is not None:
        fields.append(("retry ID", args.reply_id))
    for name, value in fields:
        if not value.strip():
            raise CliError("validation", f"A reply needs a non-empty {name}.", 2)


def _write_options(parser: argparse.ArgumentParser, idempotent_tool: str | None = None) -> None:
    parser.add_argument(
        "--input", "--json", dest="input", help="Further schema fields: strict JSON, @file or -"
    )
    if idempotent_tool:
        parser.add_argument(
            "--idempotency-key",
            help=f"Sent only if this hub's live {idempotent_tool} schema declares "
            "idempotency_key; a repeat with the same key returns the committed result",
        )


def _wait_options(parser: argparse.ArgumentParser, implied: bool = False) -> None:
    if not implied:
        parser.add_argument(
            "--wait",
            action="store_true",
            help="After commit, wait (bounded) for a terminal state; never cancels",
        )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=300.0,
        help="Wait budget in seconds (1-3600); ending it exits 10 with a durable reference",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds (0.2-60)")


def _validate_execution_bounds(args) -> None:
    bounds = [("wait_timeout", 1, 3600), ("poll_interval", 0.2, 60)]
    if args.family == "events":
        bounds = [
            ("duration", 1, 86400),
            ("poll_interval", 0.2, 60),
            ("limit", 1, 500),
            ("max_reconnects", 0, 50),
        ]
        if args.max_events is not None and args.max_events < 1:
            raise CliError("validation", "--max-events must be at least 1.", 2)
        if args.after_seq is not None and args.after_seq < 0:
            raise CliError("validation", "--after-seq must not be negative.", 2)
    for name, low, high in bounds:
        value = getattr(args, name, None)
        if value is not None and not low <= value <= high:
            flag = "--" + name.replace("_", "-")
            raise CliError("validation", f"{flag} must be between {low} and {high}.", 2)


def parser() -> Parser:
    root = Parser(
        prog="ua", description="Operator and agent client for the canonical Unified Agent hub."
    )
    _common(root, True)
    sub = root.add_subparsers(dest="family", required=True)

    def add(parent, name, help):
        child = parent.add_parser(name, help=help)
        _common(child)
        return child

    add(sub, "whoami", "Verify the authenticated identity and profile")
    add(sub, "status", "Show the selected context and authenticated identity")
    add(sub, "doctor", "Read the hub capability health report")
    context = add(sub, "context", "Inspect explicit, non-secret connection context")
    add(context.add_subparsers(dest="action", required=True), "show", "Show and verify context")

    caps = add(sub, "capabilities", "Discover the live authorized tool schemas")
    actions = caps.add_subparsers(dest="action", required=True)
    add(actions, "list", "List capabilities visible to your credential")
    add(actions, "describe", "Show a tool or client alias schema").add_argument("name")

    task = add(sub, "task", "Read, create, claim and update tasks")
    actions = task.add_subparsers(dest="action", required=True)
    listing = add(actions, "list", "Read one task page; preserve its cursor and truncation")
    listing.add_argument(
        "--status",
        default="open",
        choices=("open", "doing", "waiting_review", "blocked", "done", "cancelled", "all"),
    )
    listing.add_argument("--frontier", action="store_true")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--cursor")
    listing.add_argument("--query")
    add(actions, "show", "Read a canonical task object").add_argument("id", type=int)
    create = add(actions, "add", "Create a task (task_add)")
    create.add_argument("--title")
    create.add_argument("--notes")
    create.add_argument("--priority", help="high, med or low")
    create.add_argument("--parent", type=int, help="Parent task id")
    create.add_argument("--lane")
    _write_options(create, "task_add")
    claim = add(actions, "claim", "Claim a task as your hub-verified identity (task_claim)")
    claim.add_argument("id", type=int)
    claim.add_argument("--lane", help="Lane the claim is scoped to (expected_lane)")
    _write_options(claim)
    update = add(actions, "update", "Update a task you may change (task_update)")
    update.add_argument("id", type=int)
    update.add_argument("--status")
    update.add_argument("--note", help="Append this note (append_note)")
    update.add_argument(
        "--expected-updated-at", help="Refuse as a conflict unless the task is at this revision"
    )
    update.add_argument("--evidence-ref")
    _write_options(update, "task_update")

    inbox = add(sub, "inbox", "Read, acknowledge and answer your mail")
    actions = inbox.add_subparsers(dest="action", required=True)
    listing = add(actions, "list", "List your unread mail without marking it read")
    listing.add_argument("--all", action="store_true", help="Include already-read mail")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--after-seq", type=int)
    read = add(actions, "read", "Read your mail and mark it read (message_inbox)")
    read.add_argument("--all", action="store_true", default=None, help="Include already-read mail")
    read.add_argument("--limit", type=int)
    read.add_argument("--after-seq", type=int)
    _write_options(read)
    ack = add(actions, "ack", "Acknowledge mail (message_ack); repeating an ack is a no-op")
    target = ack.add_mutually_exclusive_group(required=True)
    target.add_argument("message_id", nargs="?")
    target.add_argument("--through", type=int, help="Ack everything up to this seq")
    _write_options(ack)
    reply = add(actions, "reply", "Send a reply you wrote yourself (message_send)")
    reply.add_argument("message_id", help="The message you answer (reply_to)")
    reply.add_argument("--to", required=True, help="Recipient agent label")
    reply.add_argument(
        "--act", default="inform", choices=("request", "inform", "query", "done", "handoff")
    )
    body = reply.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="Reply text; required, never taken from the message")
    body.add_argument("--body-file", help="Read the reply text from a file, or - for stdin")
    reply.add_argument(
        "--reply-id",
        help="Your own message_id for this reply; the hub returns the saved reply when "
        "the same sender repeats it, so a retry with it sends once",
    )
    _write_options(reply)

    run = add(sub, "run", "Inspect, wait on and intervene in dispatch runs")
    actions = run.add_subparsers(dest="action", required=True)
    add(actions, "get", "Read run state").add_argument("id")
    cancel = add(actions, "cancel", "Cancel your own live run (dispatch_cancel)")
    cancel.add_argument("id")
    cancel.add_argument("--reason")
    add(actions, "resume", "Resume your cancelled or retryable run (dispatch_resume)").add_argument(
        "id"
    )
    waiting = add(actions, "wait", "Wait, bounded, for a terminal run state; never cancels")
    waiting.add_argument("id")
    _wait_options(waiting, implied=True)

    delegate = add(sub, "delegate", "Start, inspect, wait on and cancel delegations")
    actions = delegate.add_subparsers(dest="action", required=True)
    start = add(
        actions,
        "start",
        "Delegate through the hub's gates (delegation_manage create) with a persisted retry key",
    )
    start.add_argument("--to", required=True, help="Receiving agent label")
    start.add_argument(
        "--spec-file",
        required=True,
        help="Delegation spec from a file, or - for stdin; there is no inline prompt flag",
    )
    start.add_argument("--priority", choices=("high", "med", "low"))
    start.add_argument("--parent", type=int, help="Parent task id")
    start.add_argument("--deadline", help="Advisory ISO-8601 deadline")
    start.add_argument("--callback", choices=("notify", "memory", "none"))
    key = start.add_mutually_exclusive_group()
    key.add_argument(
        "--idempotency-key",
        help="Retry key (default: generated, saved locally before sending, and reused by "
        "an identical retry)",
    )
    key.add_argument(
        "--new-key",
        action="store_true",
        help="Start a new request even if an identical one is unsettled",
    )
    _wait_options(start)
    add(actions, "lookup", "Read the hub's receipt for a key, with no effect").add_argument("key")
    add(actions, "show", "Read a canonical delegation object").add_argument("id", type=int)
    waiting = add(actions, "wait", "Wait, bounded, for a terminal delegation state; never cancels")
    waiting.add_argument("id", type=int)
    _wait_options(waiting, implied=True)
    cancel = add(actions, "cancel", "Retire a delegation you sent or received")
    cancel.add_argument("id", type=int)
    cancel.add_argument("--reason")
    cancel.add_argument(
        "--expected-claim-generation",
        type=int,
        help="Refuse unless the delegation is still at this claim generation (lease fence)",
    )
    cancel.add_argument(
        "--require-no-run", action="store_true", help="Refuse if a run is already attached"
    )

    reconcile = add(sub, "reconcile", "Resolve an unknown or parked request from its receipt key")
    reconcile.add_argument("key")
    reconcile.add_argument(
        "--retry",
        action="store_true",
        help="If the hub holds no commit, resend the saved request once with the same key",
    )
    _wait_options(reconcile)

    workflow = add(sub, "workflow", "List and run repo-reviewed workflows")
    actions = workflow.add_subparsers(dest="action", required=True)
    add(actions, "list", "List workflow definitions (workflow_list)")
    flow = add(actions, "run", "Run a workflow through its gated steps (workflow_run)")
    flow.add_argument("name")
    flow.add_argument("--inputs", help="Declared workflow inputs: strict JSON object, @file or -")
    flow.add_argument("--instance-id", help="Resume this role-workflow instance")

    events = add(sub, "events", "Follow the hub event log as resumable JSONL")
    follow = add(
        events.add_subparsers(dest="action", required=True),
        "follow",
        "Stream events after a seq cursor; reconnects from the last emitted seq",
    )
    follow.add_argument("--after-seq", type=int, help="Start after this seq (exclusive)")
    follow.add_argument("--cursor-file", help="Resume from, and save progress to, this file")
    follow.add_argument("--from-start", action="store_true", help="Start at seq 0")
    follow.add_argument("--channel", action="append", help="Channel or prefix* (repeatable)")
    follow.add_argument("--channel-prefix")
    follow.add_argument("--limit", type=int, default=100, help="Events per page (1-500)")
    follow.add_argument(
        "--duration", type=float, default=300.0, help="Stop after seconds (1-86400)"
    )
    follow.add_argument("--max-events", type=int, help="Stop after this many events")
    follow.add_argument("--once", action="store_true", help="Read one page and stop")
    follow.add_argument("--poll-interval", type=float, default=2.0, help="Seconds (0.2-60)")
    follow.add_argument(
        "--max-reconnects", type=int, default=5, help="Consecutive lost polls tolerated (0-50)"
    )
    add(sub, "needs", "List everything waiting on the operator, and where to act")
    approval = add(
        sub, "approval", "Inspect approvals; resolution uses the existing operator surface"
    )
    actions = approval.add_subparsers(dest="action", required=True)
    add(actions, "list", "List pending approvals")
    add(actions, "show", "Read the exact approval object").add_argument("id", type=int)

    evidence = add(sub, "evidence", "Read attributed evidence from the hub")
    show = add(
        evidence.add_subparsers(dest="action", required=True),
        "show",
        "Read a bounded evidence report",
    )
    show.add_argument("--task", type=int)
    show.add_argument("--pr")
    show.add_argument("--since")
    show.add_argument("--until")
    show.add_argument("--limit", type=int, default=100)

    node = add(sub, "node", "Device acceptance and node retirement (operator actions)")
    actions = node.add_subparsers(dest="action", required=True)
    add(actions, "list", "List registered ua-nodes and retired rows (who, when, why)")
    acceptance = add(
        actions, "acceptance", "Run, re-evaluate, attest or read ua-node device acceptance"
    )
    verbs = acceptance.add_subparsers(dest="verb", required=True)
    add(verbs, "start", "Queue the harmless acceptance job on a node").add_argument("node")
    add(verbs, "evaluate", "Re-evaluate and record a node's latest run").add_argument("node")
    attest = add(
        verbs,
        "attest",
        "Attest independently verified execution of a pending run; lifecycle operators only",
    )
    attest.add_argument("node")
    attest.add_argument("--run-id", required=True, help="Exact pending acceptance run id")
    attest.add_argument(
        "--evidence-ref",
        required=True,
        help="Independent verification summary (at most 1000 chars)",
    )
    add(verbs, "status", "Read one node's live result, or every stored summary").add_argument(
        "node", nargs="?"
    )
    for verb, text in (
        ("retire", "Retire a node row (kept, never deleted); lifecycle operators only"),
        ("unretire", "Return a retired node row to service; lifecycle operators only"),
    ):
        change = add(actions, verb, text)
        change.add_argument("node")
        change.add_argument(
            "--reason", required=True, help="Why; recorded on the ledger (at most 500 chars)"
        )

    command = add(sub, "command", "Schema-driven access to authorized read-only tools")
    invoke = add(
        command.add_subparsers(dest="action", required=True),
        "invoke",
        "Validate and invoke a read-only tool or alias",
    )
    invoke.add_argument("name")
    invoke.add_argument("--input", help="Strict JSON object, @file, or - for stdin")

    entry = add(sub, "mcp-entry", "Print the one hub MCP entry for a harness; changes nothing")
    entry.add_argument(
        "--harness",
        choices=("claude", "codex", "cursor", "antigravity", "opencode", "hermes"),
        help="One harness; omitted prints all of them",
    )
    return root


def _execute(args, client, identity: dict):
    family = args.family
    if family == "whoami":
        return identity
    if family == "needs":
        from . import nodes

        return nodes._call(client, "GET", "/api/attention")["needs_operator"]
    if family in {"status", "context"}:
        return {"hub": client.hub_url, **identity, "project": args.project, "session": args.session}
    if family in {"task", "run", "approval"} and args.action in {"show", "get"}:
        object_id = str(args.id)
        if (
            not object_id
            or object_id in {".", ".."}
            or any(ord(c) < 32 or ord(c) == 127 for c in object_id)
        ):
            raise CliError("validation", "Object id must be non-empty and contain no controls.", 2)
        uri = f"unified-agent://{family}s/{quote(object_id, safe='')}"
        return client.resource(uri)

    if family == "node":
        # HTTP twins, not MCP: the operator's MCP profile cannot call them (#2208).
        from . import nodes

        return nodes.execute(args, client)

    from .commands import ALIASES, arguments, describe, load_input, readable, resolve

    if family == "delegate" and args.action == "show":
        from . import execution

        return execution.delegate_show(args, client)
    if (family, getattr(args, "action", None)) in {("delegate", "wait"), ("run", "wait")}:
        from . import execution

        return (
            execution.delegate_wait(args, client)
            if family == "delegate"
            else execution.run_wait(args, client)
        )
    catalog = client.tools()
    execute = EXECUTION.get((family, getattr(args, "action", None)))
    if execute:
        return _execution(args, client, identity, resolve(execute, catalog))
    write = WRITES.get((family, getattr(args, "action", None)))
    if write:
        return _write(args, client, identity, resolve(write, catalog))
    if family == "capabilities":
        if args.action == "list":
            return {
                "discovery": "authorized-mcp-tools",
                "platform_command_version": None,
                "tools": [describe(tool) for tool in catalog],
            }
        return describe(resolve(args.name, catalog))

    if family == "command":
        name, data = args.name, load_input(args.input)
    elif family == "task":
        name = "task.list"
        data = {"status": args.status, "fields": "compact", "limit": args.limit}
        data.update(
            {k: v for k, v in {"cursor": args.cursor, "query": args.query}.items() if v is not None}
        )
        if args.frontier:
            data["frontier"] = True
    elif family == "inbox":
        name, data = "inbox.list", {"unread_only": not args.all, "limit": args.limit}
        if args.after_seq is not None:
            data["after_seq"] = args.after_seq
    elif family == "approval":
        name, data = "approval.list", {}
    elif family == "evidence":
        name = "evidence.show"
        data = {
            k: v
            for k, v in {
                "task_id": args.task,
                "pr": args.pr,
                "since": args.since,
                "until": args.until,
                "limit": args.limit,
            }.items()
            if v is not None
        }
    else:
        name, data = "doctor", {}
    tool = resolve(name, catalog)
    if not readable(tool, data):
        raise CliError(
            "read_only_client",
            "This client slice invokes only read-only calls; exports require write_to_vault=false.",
            4,
        )
    validated = arguments(tool, data, {"project": args.project, "session_id": args.session})
    return client.tool(ALIASES.get(name, name), validated)


# Controlled execution (task #1938): one existing hub tool per verb.
EXECUTION = {
    ("delegate", "start"): "delegation_manage",
    ("delegate", "lookup"): "delegation_manage",
    ("delegate", "cancel"): "delegation_manage",
    ("reconcile", None): "delegation_manage",
    ("run", "cancel"): "dispatch_cancel",
    ("run", "resume"): "dispatch_resume",
    ("workflow", "list"): "workflow_list",
    ("workflow", "run"): "workflow_run",
    ("events", "follow"): "event_log_query",
}


def _execution(args, client, identity: dict, tool: dict):
    from . import execution

    key = (args.family, getattr(args, "action", None))
    if key == ("delegate", "start"):
        return execution.delegate_start(args, client, identity, tool)
    if key == ("delegate", "lookup"):
        return execution.delegate_lookup(args, client, identity, tool)
    if key == ("delegate", "cancel"):
        return execution.delegate_cancel(args, client, identity, tool)
    if key == ("reconcile", None):
        return execution.reconcile(args, client, identity, tool)
    if key in {("run", "cancel"), ("run", "resume")}:
        return execution.run_intervene(args, client, tool)
    if key == ("workflow", "run"):
        return execution.workflow_run(args, client, tool)
    if key == ("events", "follow"):
        raise _Stream(tool)
    return client.tool(tool["name"], {})


class _Stream(Exception):
    """Hands a streaming verb back to ``run``, which owns stdout."""

    def __init__(self, tool: dict):
        super().__init__("stream")
        self.tool = tool


# Explicit verbs are the only write path; `command invoke` stays read-only.
WRITES = {
    ("task", "add"): "task_add",
    ("task", "claim"): "task_claim",
    ("task", "update"): "task_update",
    ("inbox", "read"): "coordination_manage",
    ("inbox", "ack"): "coordination_manage",
    ("inbox", "reply"): "message_send",
}


def _not_claimed(wait: dict, task_id: int) -> CliError:
    """safe_wait is a hub result, not a refusal, but the caller holds nothing (#2388)."""
    parts = [f"task {task_id} was not claimed; held by {wait.get('owner')}"]
    if wait.get("waiting_on"):
        parts.append(f"waiting on {wait['waiting_on']}")
    parts.append(f"until {wait.get('until') or 'no expiry'}")
    parts.append(f"generation {wait.get('generation')}")
    return CliError("safe_wait", safe_text(", ".join(parts)), 8, outcome="conflict", reference=wait)


def _write(args, client, identity: dict, tool: dict):
    from .commands import arguments, load_input, read_bytes, write_input

    action = args.action
    # A claim or update is guarded by the selected project, never moved into it.
    context = {"expected_project": args.project}
    if action == "add":
        flags = {
            "title": args.title,
            "notes": args.notes,
            "priority": args.priority,
            "parent_id": args.parent,
            "lane": args.lane,
        }
        context = {"project": args.project}
    elif action == "claim":
        flags = {"id": args.id, "agent": identity["identity"], "expected_lane": args.lane}
    elif action == "update":
        flags = {
            "id": args.id,
            "status": args.status,
            "append_note": args.note,
            "expected_updated_at": args.expected_updated_at,
            "evidence_ref": args.evidence_ref,
        }
    elif action == "reply":
        body = args.body
        if args.body_file is not None:
            try:
                body = read_bytes(args.body_file).decode("utf-8")
            except (OSError, UnicodeError):
                raise CliError(
                    "validation", "Reply body must be a readable UTF-8 file.", 2
                ) from None
        if not body.strip():
            raise CliError("validation", "A reply needs an explicit, non-empty body.", 2)
        flags = {
            "act": args.act,
            "to_agent": args.to,
            "body": body,
            "reply_to": args.message_id,
            "message_id": args.reply_id,
        }
        context = {"from_session": args.session}
    else:
        if action == "read":
            params = {
                "unread_only": None if args.all is None else not args.all,
                "limit": args.limit,
                "after_seq": args.after_seq,
            }
        else:
            params = {"message_id": args.message_id, "through": args.through}
        params["session_id"] = args.session
        flags = {
            "action": "message_inbox" if action == "read" else "message_ack",
            "params": {key: value for key, value in params.items() if value is not None},
        }
    key = getattr(args, "idempotency_key", None)
    if key is not None:
        if "idempotency_key" not in tool["inputSchema"].get("properties", {}):
            raise CliError(
                "validation",
                f"This hub's {tool['name']} declares no idempotency_key; it is not retry-safe.",
                2,
            )
        flags["idempotency_key"] = key
    data = load_input(getattr(args, "input", None))
    if action in {"read", "ack"}:
        supplied_params = data.get("params", {})
        if not isinstance(supplied_params, dict):
            raise CliError("validation", "Mailbox params must be a JSON object.", 2)
        if action == "ack":
            other_target = "message_id" if args.through is not None else "through"
            if supplied_params.get(other_target) is not None:
                raise CliError("validation", "Input conflicts with the selected mailbox target.", 2)
        # Protect selected action, explicit target and session, while allowing
        # additional mailbox fields alongside them. Read defaults do not mask
        # caller fields: only an explicitly supplied flag takes precedence.
        merged_params = write_input(supplied_params, flags.pop("params"))
        if action == "read":
            merged_params.setdefault("unread_only", True)
            merged_params.setdefault("limit", 50)
        data["params"] = merged_params
    data = write_input(data, flags)
    if action == "reply":
        retry_id = data.get("message_id")
        if isinstance(retry_id, str) and not retry_id.strip():
            raise CliError("validation", "A reply needs a non-empty retry ID when supplied.", 2)
    validated = arguments(tool, data, context)
    try:
        result = client.tool(tool["name"], validated, write=True)
        if action == "claim" and isinstance(result, dict) and "safe_wait" in result:
            raise _not_claimed(result["safe_wait"], args.id)
        return result
    except CliError as error:
        if error.code in {"timeout", "transport"}:
            # The request may have committed: never suggest a blind retry.
            raise CliError(
                error.code,
                "The write's outcome is unknown; read the object before retrying.",
                error.exit_code,
            ) from None
        raise


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    request_id = uuid.uuid4().hex
    # Keep parse failures machine-readable when the format selector itself is valid.
    machine = any(
        value == "--format=json"
        or value == "--format"
        and i + 1 < len(argv)
        and argv[i + 1] == "json"
        for i, value in enumerate(argv)
    )
    command = "ua"
    client = None
    try:
        args = parser().parse_args(argv)
        machine = args.format == "json"
        command = ".".join(
            part
            for part in (args.family, getattr(args, "action", None), getattr(args, "verb", None))
            if part
        )
        _validate_reply_intent(args)
        if args.timeout is not None and not 1 <= args.timeout <= 120:
            raise CliError("validation", "--timeout must be between 1 and 120 seconds.", 2)
        if args.timeout is None:
            slow_report = (
                args.family in {"doctor", "evidence", "reconcile"}
                or (
                    args.family == "command"
                    and args.name in {"doctor", "system_doctor", "evidence.show", "evidence_report"}
                )
                or (args.family, getattr(args, "action", None))
                in {("delegate", "start"), ("workflow", "run")}
            )
            args.timeout = 60.0 if slow_report else 10.0
        if args.family in {"delegate", "run", "reconcile", "events"}:
            _validate_execution_bounds(args)
        args.project = args.project or os.environ.get("UA_CLI_PROJECT")
        args.session = args.session or os.environ.get("UA_CLI_SESSION")
        if args.family == "mcp-entry":
            # Print only: no credential, no hub connection, no harness file.
            from . import harness_entry

            data = harness_entry.render_entries(args.hub, args.harness)
            print(
                json.dumps(envelope(request_id, command, data), ensure_ascii=True, allow_nan=False)
                if machine
                else harness_entry.text(data)
            )
            return 0
        # Parse/help errors never import credential resolution or compose the hub.
        from .client import ReadClient

        client = ReadClient(args.hub, args.identity, args.timeout, args.token_file)
        identity = client.whoami()
        try:
            data = _execute(args, client, identity)
        except _Stream as stream:
            from . import execution

            return execution.follow(args, client, stream.tool, machine)
        print(
            json.dumps(envelope(request_id, command, data), ensure_ascii=True, allow_nan=False)
            if machine
            else render(data)
        )
        return 0
    except CliError as error:
        if machine:
            print(json.dumps(envelope(request_id, command, error=error), ensure_ascii=True))
        print(f"ua: {error.code}: {error.message}", file=sys.stderr)
        return error.exit_code
    except SystemExit as exit:
        return int(exit.code or 0)
    except Exception:
        # This is the terminal client boundary. Unexpected exception text can
        # contain private response or credential data, so emit a fixed error.
        error = CliError("internal_error", "The CLI could not complete this request.")
        if machine:
            print(json.dumps(envelope(request_id, command, error=error), ensure_ascii=True))
        print(f"ua: {error.code}: {error.message}", file=sys.stderr)
        return error.exit_code
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                # The result and exit code are already selected. Cleanup must
                # not replace either, append a second envelope, or expose raw
                # transport exception text that may contain private data.
                print("ua: cleanup_failed: Transport cleanup could not complete.", file=sys.stderr)
