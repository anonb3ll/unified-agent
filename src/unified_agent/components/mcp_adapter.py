"""MCP tools of the ``mcp-adapter`` component.

Composition root: server.py and MCP wiring, post-init hooks, CLIs and process
entry points.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from ..tools import doctor, mcp_health
from .base import Component

COMPONENT = Component(
    name="mcp-adapter",
    tools=(
        doctor.system_doctor,
        mcp_health.mcp_health_telemetry,
    ),
)
