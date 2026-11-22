"""MCP tools of the ``dispatch`` component.

Approved dispatch, the executor daemon, ACP runners, teams and crews, routines,
holds, autonomy modes, delivery receipts.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import crew, routines
from ..tools import dispatch_report, graph
from .base import Component

COMPONENT = Component(
    name="dispatch",
    tools=(
        routines.routine_add,
        routines.routine_list,
        routines.routine_pause,
        routines.routine_fire,
        graph.dependency_list,
        graph.coordination_manage,
        graph.dependency_add,
        graph.dependency_remove,
        graph.handoff,
        graph.handoff_list,
        dispatch_report.dispatch_report_self,
        dispatch_report.dispatch_cancel,
        dispatch_report.dispatch_resume,
        dispatch_report.dispatch_retry_cancellation,
        dispatch_report.dispatch_confirm_cancellation,
        dispatch_report.dispatch_delivery_receipt,
        dispatch_report.dispatch_delivery_receipts_list,
        crew.crew_view,
        crew.crew_intervene,
    ),
)
