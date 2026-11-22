"""Component manifests: each component declares the MCP tools it registers.

The component map (docs/architecture/components.toml, kernel-split spec K1)
places every module in one of 16 components. Each component has a module here
that exports ``COMPONENT``, a :class:`Component` naming the tool functions
defined in that component's own modules. ``server.py`` composes the hub's
tool registry by concatenating ``load()``: component order, then manifest
order. Adding a tool is one line in its component's manifest; there is no
central list of tool names (K4, #2449).

Loading is conditional (K4b, #2603). ``UA_COMPONENTS`` names the components a
process loads; unset means every one, and ``kernel`` means the kernel plus the
adapter that must exist for the process to be an MCP server at all. The names
are exactly what the operator asked for: ``depends_on`` says which components
a component may import, not which must be loaded, so it is never expanded
here. A malformed, unreadable component map never shrinks that selection --
the test selector widens to every module instead (``affected_tests``).

This package is composition, so the map places it in ``mcp-adapter`` and
layers.toml places it in ``adapters``. ``Component`` lives in ``base`` so the
manifests can import it without a cycle.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterable
from types import ModuleType
from typing import Any

from .base import Component

# The fixed component order of docs/architecture/components.toml, base first.
# tests/test_component_manifests.py pins it against the map.
ORDER: tuple[str, ...] = (
    "kernel",
    "auth",
    "telemetry",
    "retrieval",
    "llm",
    "node",
    "dispatch",
    "audit",
    "cloud",
    "github",
    "chat-bridges",
    "dev-tools",
    "skills",
    "knowledge-jobs",
    "control-center",
    "mcp-adapter",
)

#: The environment variable naming the components to load (K4b).
COMPONENTS_ENV = "UA_COMPONENTS"
#: The composition root: every process is an MCP server, so this always loads.
ENTRY_COMPONENT = "mcp-adapter"
#: The base every process needs; ``kernel`` alone expands to kernel + adapter.
KERNEL_COMPONENT = "kernel"

# Manifests are imported by the name of their module, not by a static import
# block. Their edge to the modules they register comes from the component map
# instead: ``affected_tests`` and ``scripts/check_components.py`` both read
# ``docs/architecture/components.toml`` and treat it as an edge from this
# package to every declared module. Without that declared edge a computed
# import like this one hid the edge from ``server`` to every tool module, and
# a change to tools/dash.py selected 135 test files instead of 319 and
# dropped the tool-surface and server tests (task #2562).
_MANIFEST_MODULES: dict[str, str] = {name: name.replace("-", "_") for name in ORDER}


def _check_known(names: Iterable[str]) -> tuple[str, ...]:
    wanted = tuple(names)
    unknown = sorted(set(wanted) - set(ORDER))
    if unknown:
        raise ValueError(f"unknown component(s): {', '.join(unknown)}; known: {', '.join(ORDER)}")
    return wanted


def selected() -> tuple[str, ...]:
    """The components this process loads, in map order (``UA_COMPONENTS``).

    Unset or blank means every component. The entry component is always
    added, because a process with no adapter registers no MCP tools at all.
    An unknown name raises ``ValueError``, which stops start-up rather than
    serving a silently smaller surface.
    """
    raw = os.environ.get(COMPONENTS_ENV, "").strip()
    if not raw:
        return ORDER
    wanted = _check_known(name.strip() for name in raw.split(",") if name.strip())
    keep = set(wanted) | {ENTRY_COMPONENT}
    return tuple(name for name in ORDER if name in keep)


def enabled(name: str) -> bool:
    """Whether ``name`` is part of this process's selection (see :func:`selected`)."""
    return name in selected()


def partial() -> bool:
    """Whether this process loads less than every component (``UA_COMPONENTS``)."""
    return len(selected()) < len(ORDER)


def manifest(name: str) -> Component:
    """Import and return one component's manifest, checking the name it declares."""
    _check_known([name])
    module: ModuleType = importlib.import_module(f"{__name__}.{_MANIFEST_MODULES[name]}")
    component = module.COMPONENT
    if component.name != name:
        raise RuntimeError(f"{module.__name__} declares component {component.name!r}")
    return component


def load(names: Iterable[str] | None = None) -> tuple[Component, ...]:
    """Manifests in map order: every component, or only ``names``.

    ``names`` filters without reordering and without adding anything; an
    unknown name raises ``ValueError``. ``None`` uses :func:`selected`, so the
    default is the process selection rather than every component.
    """
    wanted = selected() if names is None else _check_known(names)
    return tuple(manifest(name) for name in ORDER if name in wanted)


def tools(names: Iterable[str] | None = None) -> tuple[Callable[..., Any], ...]:
    """Every tool function of the loaded manifests, in registration order."""
    return tuple(fn for component in load(names) for fn in component.tools)


__all__ = [
    "COMPONENTS_ENV",
    "ENTRY_COMPONENT",
    "KERNEL_COMPONENT",
    "ORDER",
    "Component",
    "enabled",
    "load",
    "manifest",
    "partial",
    "selected",
    "tools",
]
