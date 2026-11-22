"""MCP tools of the ``telemetry`` component.

Monitoring, scorecards, budgets, quota and usage observation, MCP runtime
telemetry, OTel export and telemetry projections.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import monitor
from .base import Component

COMPONENT = Component(
    name="telemetry",
    tools=(
        monitor.agent_activity,
        monitor.observability_manage,
        monitor.metrics_snapshot,
        monitor.check_anomalies,
        monitor.perf_summary,
        monitor.check_anomalies_and_notify,
    ),
)
