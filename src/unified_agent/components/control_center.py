"""MCP tools of the ``control-center`` component.

The web control center and its HTTP/JSON surface, pilot app, TUI, toolsets and
browser/mobile projections.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import graph_canvas, projection
from .base import Component

COMPONENT = Component(
    name="control-center",
    tools=(
        graph_canvas.graph_canvas_read,
        graph_canvas.graph_canvas_write,
        projection.browser_derive_task_reference,
    ),
)
