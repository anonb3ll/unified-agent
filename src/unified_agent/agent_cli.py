"""``unified-agent agent add|remove|list``: enroll and remove agents (task #2453).

Runs on the hub host, in-process against the hub database, like
``unified-agent db`` and ``unified-agent settings``: holding that host's
database is the operator's position. The acting identity is the configured
operator label unless ``--as`` names another lifecycle operator, and every
write still checks ``agent_lifecycle.operators()``.

The bearer ``add`` issues is printed once, to stdout, and nowhere else. This
module writes no log line, and the domain functions under it record only the
token's digest and metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_HARNESS_HINTS = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("cursor", "cursor"),
    ("antigravity", "antigravity"),
    ("agy", "antigravity"),
    ("opencode", "opencode"),
    ("hermes", "hermes"),
)


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="unified-agent agent",
        description="Add, remove and list agents on this hub (run on the hub host).",
    )
    root.add_argument(
        "--as",
        dest="actor",
        help="acting lifecycle operator (default: the configured operator label)",
    )
    sub = root.add_subparsers(dest="action", required=True)

    add = sub.add_parser("add", help="issue a bearer and seat an agent in an office")
    add.add_argument("label")
    add.add_argument("--office", required=True, help="office id or name in the org registry")
    add.add_argument("--profile", help="profile (default developer; may not exceed the default)")
    add.add_argument("--kind", choices=("local", "cloud"), default="local")
    add.add_argument("--expires-days", type=int, help="token lifetime in days (default none)")
    add.add_argument("--note", help="note recorded on the token row")
    add.add_argument(
        "--harness",
        choices=("claude", "codex", "cursor", "antigravity", "opencode", "hermes"),
        help="harness to print the registration entry for (default: guessed from the label)",
    )
    add.add_argument("--hub", help="hub /mcp URL for the entry (default UA_HTTP_HUB_URL)")
    add.add_argument(
        "--wait",
        type=float,
        default=None,
        help="seconds to wait for the first heartbeat (default 120)",
    )
    add.add_argument("--no-wait", action="store_true", help="do not wait for a heartbeat")
    add.add_argument(
        "--allow-plaintext-at-rest",
        action="store_true",
        help="store the bearer unencrypted (mode-0600 file) when UA_MANAGED_SECRET_KEYS "
        "is unset, instead of refusing",
    )

    rm = sub.add_parser("remove", help="retire an agent and revoke its credentials")
    rm.add_argument("label")
    rm.add_argument("--reason", required=True)
    rm.add_argument(
        "--force-release",
        action="store_true",
        help="release the agent's doing claims (reason recorded) instead of refusing",
    )

    ls = sub.add_parser("list", help="list agents with office, profile, kind and token dates")
    ls.add_argument("--json", action="store_true", help="print JSON")
    return root


def _guess_harness(label: str) -> str | None:
    lowered = label.casefold()
    return next((name for hint, name in _HARNESS_HINTS if hint in lowered), None)


def _registration(label: str, harness: str | None, hub: str | None) -> list[str]:
    from .cli import harness_entry
    from .cli.result import CliError

    name = harness or _guess_harness(label)
    if name is None:
        return [
            "Print the hub entry for the agent's harness with:",
            "  ua mcp-entry --harness <claude|codex|cursor|antigravity|opencode|hermes> "
            "--hub <hub-url>",
        ]
    try:
        data = harness_entry.render_entries(hub, name)
    except CliError:
        return [
            f"Print the hub entry for {name} with:",
            f"  ua mcp-entry --harness {name} --hub <hub-url>",
        ]
    entry = data["entries"][0]
    return [
        f"Register the hub for {name} in {entry['path']} (put the token where the "
        "placeholder or environment variable is; never on a command line):",
        *("  " + line for line in entry["config"].rstrip().splitlines()),
    ]


def _out(line: str = "") -> None:
    print(line, flush=True)


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _add(args: argparse.Namespace, actor: str) -> int:
    from . import agent_enrollment

    result = agent_enrollment.add_agent(
        args.label,
        office=args.office,
        actor=actor,
        profile=args.profile,
        kind=args.kind,
        expires_days=args.expires_days,
        note=args.note,
        allow_plaintext_at_rest=args.allow_plaintext_at_rest,
    )
    label = result["agent"]
    _out(
        f"Added {label}: office {result['office']}, profile {result['profile']}, "
        f"kind {result['kind']}."
    )
    _out(
        f"Token #{result['token_id']} issued {result['issued_at']}, expires "
        f"{result['expires_at'] or 'never'} (sha256 {result['token_hash_prefix']}...)."
    )
    _out()
    _out(f"Bearer token for {label}. It is shown once and cannot be read back:")
    _out()
    _out(f"  {result['token'].reveal()}")
    _out()
    for line in _registration(label, args.harness, args.hub):
        _out(line)
    for warning in result["warnings"]:
        _out(f"Note: {warning}")
    if args.no_wait:
        return 0
    timeout = agent_enrollment.DEFAULT_WAIT_S if args.wait is None else max(0.0, args.wait)
    _out()
    _out(f"Waiting up to {timeout:g}s for {label}'s first heartbeat (Ctrl-C stops waiting)...")
    try:
        seen = agent_enrollment.wait_for_heartbeat(label, result["issued_at"], timeout)
    except KeyboardInterrupt:
        _out("Stopped waiting; the agent is added.")
        return 0
    if seen is None:
        _out(
            f"No heartbeat yet. {label} is added; `unified-agent agent list` shows "
            "its last-seen time once it connects."
        )
        return 0
    _out(f"First heartbeat {seen['last_seen']} via {seen.get('last_seen_via') or 'unknown'}.")
    return 0


def _remove(args: argparse.Namespace, actor: str) -> int:
    # Retirement must cancel in-flight work; these imports register the
    # dispatch and node stop handlers (agent_lifecycle.REQUIRED_STOP_HANDLERS).
    from . import agent_enrollment, node_queue  # noqa: F401
    from .dispatch import coordinator  # noqa: F401

    result = agent_enrollment.remove_agent(
        args.label, reason=args.reason, actor=actor, force_release=args.force_release
    )
    label = result["agent"]
    for item in result["released"]:
        _out(f"Released task #{item['task_id']} (ledger event {item['event_ulid']}).")
    _out(
        f"Removed {label}: state {result['state']}, "
        f"{result['revoked_tokens']} managed token(s) and "
        f"{result['revoked_session_credentials']} session credential(s) revoked, "
        f"stored bearer {'deleted' if result['secret_deleted'] else 'not present'}, "
        f"seat {'removed' if result['seat_removed'] else 'none (not added by this command)'}."
    )
    for warning in result["warnings"]:
        _out(f"Note: {warning}")
    return 0


def _list(args: argparse.Namespace) -> int:
    from . import agent_enrollment

    rows = agent_enrollment.list_agents()
    if args.json:
        _out(json.dumps(rows, indent=2))
        return 0
    columns = (
        ("agent", "AGENT"),
        ("office", "OFFICE"),
        ("profile", "PROFILE"),
        ("kind", "KIND"),
        ("state", "STATE"),
        ("token_source", "TOKEN"),
        ("token_issued_at", "ISSUED"),
        ("token_expires_at", "EXPIRES"),
        ("last_seen", "LAST SEEN"),
    )

    def cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        if key == "token_source" and value == "managed":
            value = f"managed/{row.get('token_status')}"
        return "-" if value in (None, "") else str(value)

    table = [[header for _key, header in columns]]
    table += [[cell(row, key) for key, _header in columns] for row in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]
    for r in table:
        _out("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip())
    return 0


def run_agent_cli(argv: list[str]) -> int:
    from mcp.server.mcpserver.exceptions import ToolError

    from . import config

    args = _parser().parse_args(argv)
    actor = (args.actor or "").strip() or config.operator_label()
    if not config.db_path().is_file():
        # Never create a database: on a client host this would start a second store.
        _err(
            f"unified-agent agent {args.action}: no hub database at {config.db_path()}. "
            "Run this on the hub host, with the hub's UA_HOME."
        )
        return 2
    try:
        if args.action == "add":
            return _add(args, actor)
        if args.action == "remove":
            return _remove(args, actor)
        return _list(args)
    except ToolError as exc:
        _err(f"unified-agent agent {args.action}: {exc}")
        return 1
    except RuntimeError as exc:
        # storage.connect refuses on a host that holds no hub database.
        _err(
            f"unified-agent agent {args.action}: {exc}. Run this on the hub host, "
            "against the hub's database."
        )
        return 2
