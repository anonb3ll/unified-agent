"""Sync adapter for MCP 2.x ``Context.report_progress`` (task #823).

Registered tools stay synchronous and run in ``anyio.to_thread`` via
``elicitation.guarded_call``. Progress is bound onto a ContextVar in that
wrapper so a worker thread can call ``anyio.from_thread.run`` without the
tool functions taking a ``Context`` parameter (which would leak into the
MCP tool schema). Direct Python callers and the executor daemon see a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import anyio.from_thread

logger = logging.getLogger(__name__)

_progress_ctx: ContextVar[Any] = ContextVar("ua_mcp_progress_ctx", default=None)


@contextmanager
def progress_scope(ctx: Any) -> Iterator[None]:
    """Bind ``ctx`` for the current request so sync tools can report progress."""
    token = _progress_ctx.set(ctx)
    try:
        yield
    finally:
        _progress_ctx.reset(token)


def report_progress(
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """Report an absolute progress milestone, or no-op when no MCP request is bound.

    Values are absolute (MCP 2.x ``Context.report_progress``). Callers should
    emit a few meaningful milestones, not per-event spam.
    """
    try:
        from . import mcp_runtime_telemetry

        mcp_runtime_telemetry.record_progress(progress, total, message)
    except Exception:
        pass
    ctx = _progress_ctx.get()
    if ctx is None:
        return
    report = getattr(ctx, "report_progress", None)
    if report is None:
        return
    try:
        anyio.from_thread.run(report, progress, total, message)
    except RuntimeError:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(report(progress, total, message))
                return
        except RuntimeError:
            pass
        return
    except Exception:
        logger.debug("mcp progress report failed", exc_info=True)
