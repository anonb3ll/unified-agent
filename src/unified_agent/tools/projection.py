"""Pure browser projection receiver: focus anchor to task reference.

The receiver independently revalidates untrusted browsing context under
``projection.browser_v1``. It parses, verifies that the task exists, derives a
reference, and returns it without persistence. The contract is documented in
docs/browser-projection-contract-v1.md; producer minimization remains a
separate responsibility.
"""

from mcp.server.mcpserver.exceptions import ToolError

from .. import storage
from ..projection import browser_v1


def browser_derive_task_reference(record: dict, task_id: int, confidence: float) -> dict:
    """Validate a browser focus_anchor projection and derive a task_reference for task_id.

    `record` is the wire-format ProjectedRecord produced by a browser projector (contract v1, kind="focus_anchor") — treated as
    untrusted input and independently re-validated against the full contract
    (unified_agent/docs/browser-projection-contract-v1.md), not trusted
    just because the caller is on the same machine.

    `task_id` must name a real, non-terminal (not done/cancelled) task in the
    live queue: the contract's own task_ref regex only checks shape, not
    existence, so this tool is the place that closes that gap before a
    reference is derived. `confidence` is the caller's own similarity score
    in [0, 1] — the contract carries it but does not compute it.

    Returns the derived task_reference record in wire format
    (`projection.browser_v1.to_wire`). Nothing is persisted by this call.
    """
    try:
        anchor = browser_v1.parse_record(record)
    except browser_v1.ProjectionError as exc:
        raise ToolError(f"invalid projection record: {exc}") from None
    if anchor.kind != "focus_anchor":
        raise ToolError(f"expected a focus_anchor record, got kind={anchor.kind!r}")

    conn = storage.connect()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no task with id {task_id}")
    if row["status"] in ("done", "cancelled"):
        raise ToolError(
            f"task {task_id} is {row['status']}; cannot attach a focus-anchor "
            "reference to a terminal task"
        )

    try:
        task_reference = browser_v1.focus_anchor_to_task_reference(
            anchor, task_id=task_id, confidence=confidence
        )
    except browser_v1.ProjectionError as exc:
        raise ToolError(f"could not derive task reference: {exc}") from None

    return browser_v1.to_wire(task_reference)
