#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
from pathlib import Path


def log_debug(message):
    pass


def main_checkout(repo_root):
    """The primary checkout when `repo_root` is a linked git worktree, else None.

    Reads the `.git` file and `commondir` directly (no git subprocess): a worktree's
    `.git` is a file `gitdir: <main>/.git/worktrees/<name>`.
    """
    try:
        dotgit = repo_root / ".git"
        if not dotgit.is_file():
            return None
        text = dotgit.read_text().strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = repo_root / gitdir
        commondir = gitdir / "commondir"
        if commondir.is_file():
            common = (gitdir / commondir.read_text().strip()).resolve()
        else:
            common = gitdir.parent.parent.resolve()
        return common.parent
    except OSError:
        return None


def find_tool(name, repo_root):
    """Locate a dev tool: this checkout's `.venv`, the main checkout's, then PATH."""
    candidates = [repo_root / ".venv" / "bin" / name]
    main = main_checkout(repo_root)
    if main is not None:
        candidates.append(main / ".venv" / "bin" / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def checkout_for_push(resolved_cwd, repo_root):
    """Git checkout being pushed, when `resolved_cwd` is inside `repo_root`.

    A linked worktree (`.worktrees/task-<id>`) has its own `.git`. Gates must
    read that tree. A subdirectory of the primary checkout resolves back to
    `repo_root`. Both arguments are resolved paths.
    """
    for candidate in (resolved_cwd, *resolved_cwd.parents):
        if (candidate / ".git").exists():
            return candidate
        if candidate == repo_root:
            break
    return repo_root


def run_push_gates(check_root):
    """Run ruff and the audit against the checkout being pushed."""
    ruff_path = find_tool("ruff", check_root)
    # The audit runs the pushed checkout's own src via its scripts/run_audit.py
    # (task #2204). The venv's `unified-agent-audit` would import whichever
    # checkout the shared venv was installed from. The interpreter only
    # supplies the dependencies.
    python_path = find_tool("python3", check_root)
    missing = [name for name, path in (("ruff", ruff_path), ("python3", python_path)) if not path]
    if missing:
        # Never let a push through unchecked because the gate could not run.
        block(
            "Pre-push safety gate could not run: "
            f"{', '.join(missing)} not found in this checkout's .venv, the main "
            "checkout's .venv or PATH. Run `uv sync` in a checkout and retry."
        )

    log_debug("Running ruff check...")
    try:
        ruff_res = subprocess.run(
            [ruff_path, "check"], cwd=str(check_root), capture_output=True, text=True
        )
    except OSError as e:
        block(f"Pre-push safety gate could not run ruff: {e}")
    log_debug(f"Ruff status: {ruff_res.returncode}")
    if ruff_res.returncode != 0:
        msg = f"ruff check failed:\n{ruff_res.stdout}\n{ruff_res.stderr}"
        log_debug(msg)
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "Pre-push safety gate: linter (ruff) failed.\n"
                    f"{ruff_res.stdout[:500]}",
                }
            )
        )
        sys.exit(1)

    log_debug("Running unified-agent-audit...")
    try:
        audit_res = subprocess.run(
            [
                python_path,
                str(check_root / "scripts" / "run_audit.py"),
                ".",
                "--fail-on",
                "high",
            ],
            cwd=str(check_root),
            capture_output=True,
            text=True,
        )
    except OSError as e:
        block(f"Pre-push safety gate could not run unified-agent-audit: {e}")
    log_debug(f"Audit status: {audit_res.returncode}")
    if audit_res.returncode != 0:
        msg = f"audit failed:\n{audit_res.stdout}\n{audit_res.stderr}"
        log_debug(msg)
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "Pre-push safety gate: security audit failed "
                    f"(high severity findings or no runnable audit).\n"
                    f"{audit_res.stdout[:500] or audit_res.stderr[-500:]}",
                }
            )
        )
        sys.exit(1)

    log_debug("All checks passed! Allowing push.")
    print(json.dumps({"decision": "allow"}))
    sys.exit(0)


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(1)


def main():
    try:
        stdin_content = sys.stdin.read()

        if not stdin_content.strip():
            print(json.dumps({"decision": "allow"}), flush=True)
            sys.exit(0)

        data = json.loads(stdin_content)

        # Check command line
        cmd = None
        tool_call = data.get("toolCall") or data.get("tool_call")
        if isinstance(tool_call, dict):
            args = tool_call.get("args") or tool_call.get("arguments") or {}
            if isinstance(args, dict):
                cmd = args.get("CommandLine") or args.get("command")

        if not cmd:
            tool_input = data.get("tool_input") or data.get("toolInput") or {}
            if isinstance(tool_input, dict):
                cmd = tool_input.get("CommandLine") or tool_input.get("command")

        if not cmd:
            cmd = data.get("CommandLine") or data.get("command")

        log_debug(f"Resolved command: {cmd}")

        if not cmd or "git push" not in str(cmd):
            print(json.dumps({"decision": "allow"}), flush=True)
            sys.exit(0)

        # Resolve cwd
        cwd = None
        args = {}
        tool_call = data.get("toolCall") or data.get("tool_call")
        if isinstance(tool_call, dict):
            args = tool_call.get("args") or tool_call.get("arguments") or {}
        if isinstance(args, dict):
            cwd = args.get("Cwd") or args.get("cwd")
        if not cwd:
            tool_input = data.get("tool_input") or data.get("toolInput") or {}
            if isinstance(tool_input, dict):
                cwd = tool_input.get("Cwd") or tool_input.get("cwd")
        if not cwd:
            cwd = data.get("Cwd") or data.get("cwd")

        repo_root = Path(__file__).parent.parent
        check_root = repo_root
        log_debug(f"Resolved cwd: {cwd}, repo_root: {repo_root}")

        if cwd:
            try:
                resolved_cwd = Path(cwd).resolve()
                root = repo_root.resolve()
                if root not in (resolved_cwd, *resolved_cwd.parents):
                    log_debug(
                        f"Push is for external repository ({cwd}), skipping unified_agent gates."
                    )
                    print(json.dumps({"decision": "allow"}))
                    sys.exit(0)
                check_root = checkout_for_push(resolved_cwd, root)
            except Exception as e:
                log_debug(f"Error checking cwd: {e}")

        try:
            run_push_gates(check_root)
        except Exception as e:
            block(
                "Pre-push safety gate failed closed after an unexpected error "
                f"({type(e).__name__}: {e})."
            )

    except Exception as e:
        log_debug(f"Error in hook: {e}")
        # Before the gates run, an unexpected error stays allow: this hook sees
        # every command. Errors inside the gates fail closed above.
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)


if __name__ == "__main__":
    main()
