"""Red Team Pre-Review QA tool.

Executes autonomous pre-review testing, boundary checks, security audit,
linting, and unit test validation before a task is submitted to review.
"""

import subprocess
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, storage
from .audit import security_audit
from .lint import python_lint_and_format


def red_team_qa(path: str = ".", task_id: int | None = None) -> dict[str, Any]:
    from .. import execution_locality

    # Task #2109: the QA run executes project code (pytest), which can start an
    # agent in a child process; a devices-only hub never runs it.
    execution_locality.refuse_tool("red_team_qa")
    return _red_team_qa(path, task_id)


def _red_team_qa(path: str = ".", task_id: int | None = None) -> dict[str, Any]:
    """Execute autonomous pre-review Red Team QA validation on a codebase path.

    Performs security auditing, Python code formatting/linting, and unit test suite
    execution. Generates a risk-scored verdict (PASS, WARN, FAIL) and updates
    task notes if task_id is provided.

    Args:
        path: Workspace path to audit and test (defaults to current dir).
        task_id: Optional task ID to annotate with QA results.

    Returns:
        {
          "verdict": "PASS" | "WARN" | "FAIL",
          "security_status": str,
          "lint_status": str,
          "tests_status": str,
          "findings_summary": list[str],
          "task_annotated": bool
        }
    """
    ws_root = config.workspace_root()
    target_path = (ws_root / path).resolve()

    if not target_path.exists():
        raise ToolError(f"Target path does not exist: {target_path}")

    findings = []
    failed_checks = 0
    warn_checks = 0

    # 1. Security Audit
    sec_status = "PASS"
    try:
        sec_res = security_audit(path=path)
        sec_status = sec_res.get("status", "PASS").upper()
        if sec_status == "FAIL":
            failed_checks += 1
            findings.append(
                "Security audit returned FAIL status (high severity findings detected)."
            )
        elif sec_status == "WARN":
            warn_checks += 1
            findings.append(
                "Security audit returned WARN status (medium severity findings detected)."
            )
    except Exception as e:
        sec_status = "ERROR"
        warn_checks += 1
        findings.append(f"Security audit check failed to execute: {e}")

    # 2. Python Formatting & Linting
    lint_status = "PASS"
    try:
        lint_res = python_lint_and_format(path=path)
        if lint_res.get("fixed", 0) > 0:
            warn_checks += 1
            findings.append(f"Linting formatted {lint_res['fixed']} issue(s).")
    except Exception as e:
        lint_status = "ERROR"
        warn_checks += 1
        findings.append(f"Linting check failed: {e}")

    # 3. Unit Test Execution
    tests_status = "PASS"
    try:
        test_run = subprocess.run(
            ["uv", "run", "pytest", "-q"],
            cwd=str(target_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if test_run.returncode != 0:
            tests_status = "FAIL"
            failed_checks += 1
            stderr_snippet = test_run.stderr.strip()[-300:] or test_run.stdout.strip()[-300:]
            findings.append(
                f"Unit test suite failed (exit code {test_run.returncode}): {stderr_snippet}"
            )
    except subprocess.TimeoutExpired:
        tests_status = "TIMEOUT"
        failed_checks += 1
        findings.append("Unit test suite timed out after 120 seconds.")
    except Exception as e:
        tests_status = "ERROR"
        warn_checks += 1
        findings.append(f"Unit test execution error: {e}")

    # Determine Overall Verdict
    if failed_checks > 0:
        verdict = "FAIL"
    elif warn_checks > 0:
        verdict = "WARN"
    else:
        verdict = "PASS"

    task_annotated = False
    if task_id is not None:
        now_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        annotation = f"[red-team-qa: {verdict} @ {now_iso}]"

        conn = storage.connect()
        try:
            storage.begin_task_write(conn)  # fence the revision read with its write
            cur = conn.execute("SELECT notes, updated_at FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if row:
                existing_notes = row["notes"] or ""
                new_notes = f"{existing_notes}\n{annotation}".strip()
                conn.execute(
                    "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
                    (new_notes, storage.next_revision(row["updated_at"]), task_id),
                )
                conn.commit()
                task_annotated = True
        finally:
            conn.close()

    return {
        "verdict": verdict,
        "security_status": sec_status,
        "lint_status": lint_status,
        "tests_status": tests_status,
        "findings_summary": findings if findings else ["All QA checks passed cleanly."],
        "task_annotated": task_annotated,
    }
