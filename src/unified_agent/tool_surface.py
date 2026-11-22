"""The hub's governed tool surface, reachable without importing the MCP adapter.

``server.py`` registers every hub tool here, each wrapped by the hub's policy
guard (access, profile, policy and approval gates, rate limits, telemetry), and
installs that guard. Code below the adapter layer (the workflow engine, the
LangGraph dispatcher, the chat worker, the skill catalog, the capability
inventory) calls tools through this module. A call made that way takes exactly
the path an MCP call takes, and the caller does not depend on the MCP server.

``server.py`` is the composition root: importing it fills the registry. A
process that asks for a tool before anything has imported it gets it loaded on
first use by ``_load``. That one lazy import is the only place below the
adapter layer that names the server, and docs/architecture/layers.toml declares
it as a seam.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

# Name -> governed callable. ``server.REGISTERED_TOOLS`` is this same dict.
REGISTERED_TOOLS: dict[str, Callable[..., Any]] = {}

_guard: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None

# True while a registered tool runs (an MCP call or any governed call made
# through this module). Lets in-process system paths tell themselves apart
# from the same function reached as a tool (task #1775).
_IN_TOOL_CALL: ContextVar[bool] = ContextVar("ua_in_governed_tool_call", default=False)


def in_tool_call() -> bool:
    """Whether the current code runs inside a registered (governed) tool call."""
    return _IN_TOOL_CALL.get()


def _mark_tool_call(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def marked_async(*args: Any, **kwargs: Any) -> Any:
            token = _IN_TOOL_CALL.set(True)
            try:
                return await fn(*args, **kwargs)
            finally:
                _IN_TOOL_CALL.reset(token)

        return marked_async

    @functools.wraps(fn)
    def marked(*args: Any, **kwargs: Any) -> Any:
        token = _IN_TOOL_CALL.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _IN_TOOL_CALL.reset(token)

    return marked


def install_guard(guard: Callable[[Callable[..., Any]], Callable[..., Any]]) -> None:
    """Set the policy guard that wraps every hub tool (called by ``server.py``)."""
    global _guard
    _guard = guard


def register(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` in the installed guard and register it under its name."""
    wrapped = _mark_tool_call(guard(fn))
    REGISTERED_TOOLS[fn.__name__] = wrapped
    return wrapped


def registry() -> dict[str, Callable[..., Any]]:
    """Every registered governed tool, loading the hub surface on first use."""
    if not REGISTERED_TOOLS:
        _load()
    return REGISTERED_TOOLS


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` in the hub's policy guard, exactly as registration does."""
    if _guard is None:
        _load()
    if _guard is None:  # pragma: no cover - server.py always installs it
        raise RuntimeError("the hub tool surface did not install a policy guard")
    return _guard(fn)


def _load() -> None:
    # The composition root registers every tool and installs the guard at
    # import. Declared as a seam in docs/architecture/layers.toml.
    from . import server  # noqa: F401
