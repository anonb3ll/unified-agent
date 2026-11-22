"""Same-process change notices for live views of tasks and runs.

Domain code calls ``task_changed`` or ``runs_changed`` after a committed write.
Live views subscribe: the MCP live-state adapter (``mcp_live_state``) turns a
notice into resource-updated messages for subscribed clients. Domain code
therefore never imports the MCP adapter.

Notices are best-effort. A listener that raises is skipped, and with no
listener (a CLI or worker that never loaded the MCP server) a notice does
nothing, which is what publishing did there before: no event loop, no clients.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# A listener receives the kind of object ("task" or "run") and the ids that
# changed. For "task", an empty tuple means "the board changed".
Listener = Callable[[str, tuple], None]

_listeners: list[Listener] = []


def subscribe(listener: Listener) -> None:
    """Register ``listener`` once; repeated subscriptions are ignored."""
    if listener not in _listeners:
        _listeners.append(listener)


def unsubscribe(listener: Listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def task_changed(task_id: int | None = None) -> None:
    """A task was created, updated or deleted (``None``: only the board changed)."""
    _emit("task", () if task_id is None else (task_id,))


def runs_changed(run_ids: Iterable[str | None]) -> None:
    """One or more run records changed."""
    ids = tuple(dict.fromkeys(r for r in run_ids if r))
    if ids:
        _emit("run", ids)


def _emit(kind: str, ids: tuple) -> None:
    for listener in tuple(_listeners):
        try:
            listener(kind, ids)
        except Exception:  # noqa: BLE001 - notices are best-effort
            logger.debug("change listener failed for %s %s", kind, ids, exc_info=True)
