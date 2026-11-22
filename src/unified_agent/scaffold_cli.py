"""CLI tool to scaffold a new unified-agent satellite project.

Stamps out a project-scoped ``.mcp.json`` (its own ``UA_WORKSPACE``, so
``fs_*``/``run_cmd`` tools sandbox to just that project) and runs
``unified-agent-sync``, so wiring up a new satellite project — or a new tool
someone is experimenting with — is one command instead of hand-editing JSON
from memory.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .sync_cli import detect_client

_INVALID_NAME_CHARS = ("/", "\\")
_INVALID_NAMES = (".", "..")


def find_sibling_binary(name: str) -> Path | None:
    """Look for another console-script binary installed alongside this one."""
    candidate = Path(sys.executable).parent / name
    return candidate if candidate.exists() else None


def validate_name(name: str) -> str | None:
    """Return an error message if `name` is unsafe as a dir/server name, else None.

    `name` feeds both the default target directory (Path.home()/"dev"/name) and
    the MCP server name (unified-agent-<name>) — reject anything that could
    escape the intended directory or produce a nonsensical server name.
    """
    if not name or not name.strip():
        return "name must not be empty"
    if name in _INVALID_NAMES:
        return f"name must not be {name!r}"
    if any(c in name for c in _INVALID_NAME_CHARS):
        return "name must not contain '/' or '\\\\' — pass a target directory via --path instead"
    return None


def write_mcp_json(target: Path, project_name: str, ua_bin: Path) -> str:
    """Write a project-scoped .mcp.json pointing UA_WORKSPACE at target.

    Returns the MCP server name used.
    """
    server_name = f"unified-agent-{project_name}"
    config = {
        "mcpServers": {
            server_name: {
                "command": str(ua_bin),
                "env": {"UA_WORKSPACE": str(target)},
            }
        }
    }
    target.joinpath(".mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return server_name


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold a new unified-agent satellite project: a project-scoped "
        ".mcp.json plus the ua-sync managed context block, in one command."
    )
    parser.add_argument("name", help="Short project name (default dir name + MCP server name)")
    parser.add_argument(
        "--path",
        type=str,
        help="Target directory (default: ~/dev/<name>). May already exist — "
        "existing content is left alone.",
    )
    parser.add_argument(
        "--client",
        choices=["claude", "agy", "codex", "hermes"],
        help="Passed through to unified-agent-sync; omit to auto-detect (an explicit client is required if detection fails)",
    )
    parser.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        help="Print what would happen; write nothing",
    )
    args = parser.parse_args()

    name_error = validate_name(args.name)
    if name_error is not None:
        print(f"error: {name_error}", file=sys.stderr)
        sys.exit(1)

    target = Path(args.path).resolve() if args.path else Path.home() / "dev" / args.name
    mcp_json = target / ".mcp.json"

    if target.exists() and not target.is_dir():
        print(f"error: {target} exists and is not a directory", file=sys.stderr)
        sys.exit(1)

    if mcp_json.exists():
        print(f"error: {mcp_json} already exists — refusing to overwrite.", file=sys.stderr)
        print("  Edit it by hand if you need to change its UA_WORKSPACE.", file=sys.stderr)
        sys.exit(1)

    ua_bin = find_sibling_binary("unified-agent")
    if ua_bin is None:
        print("error: unified-agent binary not found alongside this interpreter", file=sys.stderr)
        sys.exit(1)

    server_name = f"unified-agent-{args.name}"
    sync_bin = find_sibling_binary("unified-agent-sync")
    resolved_client = args.client or (detect_client() if sync_bin is not None else None)

    if args.dry_run:
        if target.exists():
            print(f"Would use existing directory: {target}")
        else:
            print(f"Would create: {target} (git-initialized)")
        print(f"Would write: {mcp_json} (UA_WORKSPACE={target}, server: {server_name})")
        if sync_bin is None:
            print(
                "Would skip context sync: unified-agent-sync not found alongside this interpreter"
            )
        else:
            sync_cmd = f"unified-agent-sync --path {target} --project {args.name}"
            if resolved_client:
                sync_cmd += f" --client {resolved_client}"
            print(f"Would run: {sync_cmd}")
        return

    if sync_bin is not None and resolved_client is None:
        print(
            "error: unified-agent-sync requires an explicit --client when scaffolding; "
            "refusing to guess identity before writing files",
            file=sys.stderr,
        )
        sys.exit(2)

    created_dir = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if created_dir:
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(target), check=True)
        except subprocess.CalledProcessError as e:
            print(f"error: 'git init' failed in {target}: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"Created {target} (git-initialized — no remote; add one with 'git remote add origin <url>')"
        )
    else:
        print(f"Using existing directory: {target}")

    write_mcp_json(target, args.name, ua_bin)
    print(f"Wrote {mcp_json} (UA_WORKSPACE={target}, server name: {server_name})")

    if sync_bin is None:
        print("warning: unified-agent-sync not found — skipping context sync.", file=sys.stderr)
    else:
        sync_args = [str(sync_bin), "--path", str(target), "--project", args.name]
        sync_args += ["--client", resolved_client]
        try:
            subprocess.run(sync_args, check=True)
        except subprocess.CalledProcessError as e:
            print(f"error: unified-agent-sync failed: {e}", file=sys.stderr)
            sys.exit(1)

    print(
        f"\nDone. Next steps:\n"
        f"  cd {target}\n"
        f"  claude              # (or antigravity / hermes) — .mcp.json loads this project's tools automatically\n"
        f"  claude mcp list     # confirm '{server_name}' is registered"
    )


if __name__ == "__main__":
    main()
