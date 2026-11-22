"""Which tasks are role-workflow steps: the read the kernel's task writers need (#2450).

``workflow_roles`` (skills) projects a role workflow onto ordinary task rows and
owns every write to its tables. A kernel writer that completes a task directly
(``run_records``) only needs to know whether the task is a workflow step, so it
can leave the step to an explicit workflow closure. That read is pure SQL over
tables the kernel already creates (``table_schemas.WORKFLOW_ROLES``), so it
lives here and ``workflow_roles`` re-exports it under its old names.
"""

from __future__ import annotations


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_instances'").fetchone()


def is_workflow_task(conn, task_id):
    """Direct completion writers must leave role steps to explicit closures."""
    return bool(
        _exists(conn)
        and conn.execute("SELECT 1 FROM workflow_task_steps WHERE task_id=?", (task_id,)).fetchone()
    )
