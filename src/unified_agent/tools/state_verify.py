"""Automated verification tool to check and restore parity between SQLite database state and Obsidian vault exports."""

import json

from .. import config, storage
from ..sync import resolve_in_vault, sync_state_now


def _mirror_path(sync_dir: str, stem: str):
    """This host's state mirror, falling back to the legacy unscoped filename.

    Mirrors became host-scoped in task #172 (`state_tasks.DesktopHost.txt`).
    A vault written before that change still holds `state_tasks.txt`, and
    without the fallback the first verify after upgrading would report drift
    against a file that is present and correct — then resync on every call.

    The host-scoped file wins whenever it exists: the fallback exists to cover
    the gap before the first new write, not to let a stale legacy snapshot mask
    the current one.
    """
    scoped = resolve_in_vault(
        f"{sync_dir}/{config.state_mirror_name(stem, '.txt')}", for_write=False
    )
    if scoped.exists():
        return scoped
    legacy = resolve_in_vault(f"{sync_dir}/{stem}.txt", for_write=False)
    return legacy if legacy.exists() else None


def state_sync_verify() -> dict:
    """Verify parity between live SQLite database tables (memory, tasks) and Obsidian vault state exports (Logs/state_*.txt).

    If drift is detected (e.g. missing files or mismatched item counts), an immediate
    sync is triggered to restore state parity.

    Returns:
        {
          "in_sync": bool,
          "memory_db_count": int,
          "memory_file_count": int,
          "task_db_count": int,
          "task_file_count": int,
          "resynced": bool,
          "drift_details": dict
        }
    """
    root = config.vault_root()
    if not root or not root.exists():
        return {
            "in_sync": False,
            "memory_db_count": 0,
            "memory_file_count": 0,
            "task_db_count": 0,
            "task_file_count": 0,
            "resynced": False,
            "drift_details": {"error": "Vault root directory not configured or does not exist"},
        }

    # 1. Fetch DB counts & IDs
    conn = storage.connect()
    try:
        mem_rows = conn.execute(
            "SELECT id FROM memory WHERE id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%'"
        ).fetchall()
        db_mem_ids = {r["id"] for r in mem_rows}

        task_rows = conn.execute("SELECT id FROM tasks").fetchall()
        db_task_ids = {r["id"] for r in task_rows}
    finally:
        conn.close()

    # 2. Read Vault JSON export files
    file_mem_ids: set[str] = set()
    file_task_ids: set[int] = set()
    memory_file_exists = False
    tasks_file_exists = False

    try:
        sync_dir = config.state_sync_dir()
        mem_file_path = _mirror_path(sync_dir, "state_memory")
        if mem_file_path is not None:
            memory_file_exists = True
            with open(mem_file_path, encoding="utf-8") as f:
                mem_data = json.load(f)
                file_mem_ids = {item["id"] for item in mem_data}
    except Exception:
        pass

    try:
        task_file_path = _mirror_path(sync_dir, "state_tasks")
        if task_file_path is not None:
            tasks_file_exists = True
            with open(task_file_path, encoding="utf-8") as f:
                task_data = json.load(f)
                file_task_ids = {item["id"] for item in task_data}
    except Exception:
        pass

    mem_drift = (not memory_file_exists) or (db_mem_ids != file_mem_ids)
    task_drift = (not tasks_file_exists) or (db_task_ids != file_task_ids)

    in_sync = not (mem_drift or task_drift)
    resynced = False

    if not in_sync:
        sync_state_now()
        resynced = True

    return {
        "in_sync": in_sync,
        "memory_db_count": len(db_mem_ids),
        "memory_file_count": len(file_mem_ids),
        "task_db_count": len(db_task_ids),
        "task_file_count": len(file_task_ids),
        "resynced": resynced,
        "drift_details": {
            "memory_drift": mem_drift,
            "task_drift": task_drift,
            "memory_file_missing": not memory_file_exists,
            "tasks_file_missing": not tasks_file_exists,
        },
    }
