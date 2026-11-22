"""Read-only delivery accountability receipts over existing dispatch records."""

from __future__ import annotations

from typing import Any

from .. import storage
from ..delivery_receipts import (  # noqa: F401 - receipt types and readers; re-exported here
    _TERMINAL_RUN_STATES,
    DeliveryReceipt,
    ExecutorAuthProof,
    _build_receipt,
    _transition_rows,
    receipts_for_task,
)


def _executor_auth_proof() -> ExecutorAuthProof:
    """Reuse the existing standalone daemon-client doctor probe (service_probes, #1722)."""
    try:
        from .. import service_probes

        check = service_probes._active_executor_check()
        status = str(check.get("status") or "UNKNOWN").upper()
        if status not in {"LIVE", "DORMANT", "MISCONFIGURED", "UNKNOWN"}:
            status = "UNKNOWN"
        detail = str(check.get("detail") or "")
        if "probe_process=separate" in detail:
            process = "separate"
        elif "hub-served system_doctor call" in detail:
            process = "hub_skipped"
        else:
            process = "not_run"
        authenticated = True if status == "LIVE" and process == "separate" else None
        if status == "MISCONFIGURED" and process == "separate":
            authenticated = False
        return {
            "status": status,
            "authenticated": authenticated,
            "probe_process": process,
        }
    except Exception:
        return {"status": "UNKNOWN", "authenticated": None, "probe_process": "not_run"}


def build_delivery_receipt(
    run_id: str,
    *,
    include_executor_auth: bool = True,
    conn: Any = None,
) -> DeliveryReceipt | None:
    """Build one typed receipt without mutating any task, run, or event row."""
    rid = (run_id or "").strip()
    if not rid:
        return None
    close_conn = conn is None
    conn = conn or storage.connect()
    try:
        run_row = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
        if run_row is None:
            return None
        run = {key: run_row[key] for key in run_row.keys()}
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
        task = {key: task_row[key] for key in task_row.keys()} if task_row else {}
        return _build_receipt(
            run,
            task=task,
            transitions=_transition_rows(conn, rid),
            conn=conn,
            executor_auth=_executor_auth_proof() if include_executor_auth else None,
        )
    finally:
        if close_conn:
            conn.close()
